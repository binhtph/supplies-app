import os
import sqlite3
import json
import asyncio
import csv
import unicodedata
import re
import threading
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import paho.mqtt.client as mqtt
from fastapi import FastAPI, Request, Form, BackgroundTasks, UploadFile, File
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import urllib.parse

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

app = FastAPI()
templates = Jinja2Templates(directory="templates")

os.makedirs("data/avatars", exist_ok=True)
app.mount("/avatars", StaticFiles(directory="data/avatars"), name="avatars")
os.makedirs("static/css", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

DB_PATH = "data/app.db"
CSV_SUPPLIES = "data/supplies_export.csv"
CSV_VEHICLES = "data/vehicles_export.csv"

db_lock = threading.RLock()
mqtt_status = "Đang khởi tạo..."

def slugify(text):
    text = text.lower().replace('đ', 'd')
    text = unicodedata.normalize('NFD', text)
    text = ''.join([c for c in text if unicodedata.category(c) != 'Mn']).strip()
    text = ''.join([c if c.isalnum() else '_' for c in text])
    return re.sub(r'_+', '_', text).strip('_')

def get_unique_slugs(items, name_key='name'):
    """Tạo slug duy nhất và tên hiển thị. Trùng tên thì thêm _1, _2... vào slug và (1), (2)... vào tên."""
    result = {}
    counts = {}
    sorted_items = sorted(items, key=lambda x: x['id'])
    for item in sorted_items:
        base = slugify(item[name_key])
        if base not in counts:
            counts[base] = 0
            result[item['id']] = {'slug': base, 'name': item[name_key]}
        else:
            counts[base] += 1
            result[item['id']] = {
                'slug': f"{base}_{counts[base]}",
                'name': f"{item[name_key]} ({counts[base]})"
            }
    return result

def parse_date_to_iso(date_str):
    if not date_str: return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try: return datetime.strptime(date_str.strip(), fmt)
        except ValueError: continue
    return None

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # Cho phép đọc/ghi song song, tránh database locked
    conn.execute("PRAGMA busy_timeout=5000")  # Chờ 5 giây nếu DB bị khóa
    return conn

def log_audit(category, description):
    try:
        with db_lock:
            db = get_db()
            db.execute("INSERT INTO audit_logs (timestamp, category, description) VALUES (?, ?, ?)",
                       (datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S"), category, description))
            db.commit()
            db.close()
    except Exception as e: print(f"Lỗi ghi log: {e}")

def generate_permanent_slug_and_name(existing_items, new_name):
    base_slug = slugify(new_name)
    existing_slugs = [i['mqtt_slug'] for i in existing_items]
    
    if base_slug not in existing_slugs:
        return base_slug, new_name
    
    count = 1
    while True:
        candidate_slug = f"{base_slug}_{count}"
        if candidate_slug not in existing_slugs:
            return candidate_slug, f"{new_name} ({count})"
        count += 1

def generate_unique_group_name(existing_groups, new_name):
    base_slug = slugify(new_name)
    existing_slugs = [slugify(g['name']) for g in existing_groups]
    
    if base_slug not in existing_slugs:
        return new_name
        
    count = 1
    while True:
        candidate_name = f"{new_name} ({count})"
        candidate_slug = slugify(candidate_name)
        if candidate_slug not in existing_slugs:
            return candidate_name
        count += 1

def migrate_db():
    conn = get_db()
    try:
        conn.execute("SELECT mqtt_slug FROM vehicles LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE vehicles ADD COLUMN mqtt_slug TEXT")
        conn.execute("ALTER TABLE vehicles ADD COLUMN display_name TEXT")
        conn.execute("ALTER TABLE equipment ADD COLUMN mqtt_slug TEXT")
        conn.execute("ALTER TABLE equipment ADD COLUMN display_name TEXT")
        conn.execute("ALTER TABLE parts ADD COLUMN mqtt_slug TEXT")
        conn.execute("ALTER TABLE parts ADD COLUMN display_name TEXT")
        conn.commit()
        
        all_v = conn.execute("SELECT * FROM vehicles").fetchall()
        v_info = get_unique_slugs(all_v)
        for v in all_v:
            conn.execute("UPDATE vehicles SET mqtt_slug=?, display_name=? WHERE id=?", 
                         (v_info[v['id']]['slug'], v_info[v['id']]['name'], v['id']))

        groups = conn.execute("SELECT id FROM groups").fetchall()
        for grp in groups:
            equips = conn.execute("SELECT * FROM equipment WHERE group_id = ?", (grp['id'],)).fetchall()
            e_info = get_unique_slugs(equips)
            for e in equips:
                conn.execute("UPDATE equipment SET mqtt_slug=?, display_name=? WHERE id=?", 
                             (e_info[e['id']]['slug'], e_info[e['id']]['name'], e['id']))

        for v in all_v:
            parts = conn.execute("SELECT * FROM parts WHERE vehicle_id = ?", (v['id'],)).fetchall()
            p_info = get_unique_slugs(parts)
            for p in parts:
                conn.execute("UPDATE parts SET mqtt_slug=?, display_name=? WHERE id=?", 
                             (p_info[p['id']]['slug'], p_info[p['id']]['name'], p['id']))
        conn.commit()
    conn.close()

with db_lock:
    migrate_db()

# ================= CSV EXPORT =================
def export_csv_task():
    try:
        with db_lock:
            db = get_db()
            
            # Export Supplies
            equipments = db.execute('''
                SELECT e.*, g.name as loc_name 
                FROM equipment e JOIN groups g ON e.group_id = g.id
            ''').fetchall()
            with open(CSV_SUPPLIES, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["ID", "Nhóm", "Tên Vật Tư", "Chu Kỳ (Ngày)", "Ngày Thay Gần Nhất", "Chi Phí (VNĐ)", "Ghi Chú", "Link Mua"])
                for eq in equipments:
                    history = db.execute("SELECT replace_date, cost FROM history WHERE equipment_id = ? ORDER BY id DESC LIMIT 1", (eq['id'],)).fetchone()
                    rd = history['replace_date'] if history else "Chưa có"
                    cost = history['cost'] if history else 0
                    writer.writerow([eq['id'], eq['loc_name'], eq['name'], eq['lifetime_days'], rd, cost, eq['notes'], eq['purchase_link']])
            
            # Export Vehicles
            vehicles = db.execute("SELECT id, name FROM vehicles").fetchall()
            veh_stats = {}
            for v in vehicles:
                logs = db.execute("SELECT odo FROM odo_logs WHERE vehicle_id = ? ORDER BY log_date ASC, id ASC", (v['id'],)).fetchall()
                veh_stats[v['id']] = logs[-1]['odo'] if logs else 0
            
            parts_info = db.execute('''
                SELECT p.*, v.name as v_name 
                FROM parts p JOIN vehicles v ON p.vehicle_id = v.id
            ''').fetchall()
            with open(CSV_VEHICLES, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["ID", "Tên Xe", "ODO Hiện Tại", "Tên Phụ Tùng", "Chu Kỳ (Km)", "Ngày Thay", "ODO Lúc Thay", "Chi Phí", "Ghi Chú", "Link Mua"])
                for p in parts_info:
                    history = db.execute("SELECT replace_date, replace_odo, cost FROM part_history WHERE part_id = ? ORDER BY id DESC LIMIT 1", (p['id'],)).fetchone()
                    rd = history['replace_date'] if history else "Chưa có"
                    ro = history['replace_odo'] if history else "Chưa có"
                    cost = history['cost'] if history else 0
                    current_odo = veh_stats.get(p['vehicle_id'], 0)
                    writer.writerow([p['id'], p['v_name'], current_odo, p['name'], p['lifetime_km'], rd, ro, cost, p['notes'], p['purchase_link']])
            
            db.close()
    except Exception as e: print(f"Lỗi xuất CSV: {e}")

# ================= MATH LOGIC =================
def get_metrics(equip, db):
    history = db.execute("SELECT * FROM history WHERE equipment_id = ? ORDER BY id DESC LIMIT 1", (equip['id'],)).fetchall()
    cost = history[0]['cost'] if history else 0
    replace_date_str = history[0]['replace_date'] if history else None
    now_date = datetime.now(VN_TZ).date()

    if replace_date_str:
        replace_date_obj = datetime.strptime(replace_date_str, "%Y-%m-%d").date()
        days_used = max(0, (now_date - replace_date_obj).days)
        days_left = equip['lifetime_days'] - days_used
        next_date_obj = replace_date_obj + timedelta(days=equip['lifetime_days'])
        next_date = next_date_obj.strftime("%d/%m/%Y")
        next_date_original = next_date
        if days_left <= 0: next_date = "Cần thay ngay!"
        replace_date_display = replace_date_obj.strftime("%d/%m/%Y")
    else:
        days_used = 0
        days_left = equip['lifetime_days']
        next_date = "Chưa có"
        next_date_original = "Chưa có"
        replace_date_display = "Chưa rõ"
        
    percent = max(0, min(100, int((days_left / equip['lifetime_days']) * 100))) if equip['lifetime_days'] > 0 else 0
    need_replace = replace_date_str is not None and days_left <= 0

    return {
        "days_left": days_left, "days_used": days_used, "percent": percent,
        "next_date": next_date, "next_date_original": next_date_original, 
        "replace_date": replace_date_display, "replace_date_raw": replace_date_str, 
        "cost": cost, "need_replace": need_replace
    }

def get_vehicle_stats(vehicle_id, db):
    logs = db.execute("SELECT * FROM odo_logs WHERE vehicle_id = ? ORDER BY log_date ASC, id ASC", (vehicle_id,)).fetchall()
    if not logs: return {"current_odo": 0, "avg_km_day": 15, "last_update": "Chưa có"}
    current_odo = logs[-1]['odo']
    last_update = datetime.strptime(logs[-1]['log_date'], "%Y-%m-%d").strftime("%d/%m/%Y")
    if len(logs) == 1: return {"current_odo": current_odo, "avg_km_day": 15, "last_update": last_update}
    first_log = logs[0]
    last_log = logs[-1]
    days = (datetime.strptime(last_log['log_date'], "%Y-%m-%d").date() - datetime.strptime(first_log['log_date'], "%Y-%m-%d").date()).days
    avg_km_day = (last_log['odo'] - first_log['odo']) / days if days > 0 and (last_log['odo'] - first_log['odo']) > 0 else 15
    return {"current_odo": current_odo, "avg_km_day": round(avg_km_day, 1), "last_update": last_update}

def calculate_part_metrics(part, current_odo, avg_km_day, db):
    history = db.execute("SELECT * FROM part_history WHERE part_id = ? ORDER BY id DESC LIMIT 1", (part['id'],)).fetchall()
    cost = history[0]['cost'] if history else 0
    replace_odo = history[0]['replace_odo'] if history else current_odo
    replace_date_str = history[0]['replace_date'] if history else None
    km_used = max(0, current_odo - replace_odo)
    km_left = part['lifetime_km'] - km_used
    if part['lifetime_km'] > 0:
        if km_left >= part['lifetime_km']: km_left = part['lifetime_km']; percent = 100
        else: percent = max(0, min(100, int((km_left / part['lifetime_km']) * 100)))
    else: percent = 0
        
    logs = db.execute("SELECT log_date FROM odo_logs WHERE vehicle_id = ? ORDER BY log_date DESC, id DESC LIMIT 1", (part['vehicle_id'],)).fetchall()
    last_update_date = parse_date_to_iso(logs[0]['log_date']) if logs else datetime.now()
    if not last_update_date:
        last_update_date = datetime.now()

    if avg_km_day > 0 and km_left > 0:
        days_left_from_last = min(36500, int(km_left / avg_km_day))
        next_date_obj = last_update_date + timedelta(days=days_left_from_last)
        days_left = max(0, (next_date_obj.date() - datetime.now().date()).days)
        if next_date_obj.date() <= datetime.now().date():
            next_date = "Cần thay ngay!"
            days_left = 0
        else:
            next_date = next_date_obj.strftime("%d/%m/%Y")
    else:
        days_left = 0
        next_date = "Cần thay ngay!" if km_left <= 0 else "Chưa rõ"

    replace_date_fmt = datetime.strptime(replace_date_str, "%Y-%m-%d").strftime("%d/%m/%Y") if replace_date_str else "Chưa có"

    return {
        "km_left": km_left, "km_used": km_used, "percent": percent, "days_left": days_left,
        "next_date": next_date, "replace_odo": replace_odo, "replace_date": replace_date_fmt,
        "replace_date_raw": replace_date_str if replace_date_str else datetime.now().strftime("%Y-%m-%d"),
        "cost": cost, "history_id": history[0]['id'] if history else None
    }

# ================= MQTT LOGIC =================
def on_connect(client, userdata, flags, rc):
    global mqtt_status
    if rc == 0: mqtt_status = "Connected"
    elif rc == 1: mqtt_status = "Unacceptable protocol version"
    elif rc == 2: mqtt_status = "Identifier rejected"
    elif rc == 3: mqtt_status = "Server unavailable"
    elif rc == 4: mqtt_status = "Bad user name or password"
    elif rc == 5: mqtt_status = "Not authorized"
    else: mqtt_status = f"Lỗi kết nối ({rc})"
    log_audit("MQTT", f"Status: {mqtt_status}")

def on_disconnect(client, userdata, rc):
    global mqtt_status
    mqtt_status = "Disconnected"
    log_audit("MQTT", "Disconnected")

def sync_mqtt_now():
    try:
        client = mqtt.Client()
        # Dùng callback cục bộ, không ảnh hưởng global mqtt_status
        def _on_connect(c, u, f, rc):
            global mqtt_status
            if rc == 0: 
                mqtt_status = "Connected"
            else:
                codes = {1:"Sai giao thức",2:"ID bị từ chối",3:"Server không sẵn sàng",4:"Sai user/pass",5:"Không được phép"}
                mqtt_status = codes.get(rc, f"Lỗi kết nối ({rc})")
        def _on_disconnect(c, u, rc):
            pass  # Bỏ qua disconnect sau khi sync xong - không reset status
        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        if os.getenv("MQTT_USER"): client.username_pw_set(os.getenv("MQTT_USER"), os.getenv("MQTT_PASSWORD"))
        client.connect(os.getenv("MQTT_BROKER", "localhost"), int(os.getenv("MQTT_PORT", 1883)), 60)
        client.loop_start() 
        
        with db_lock:
            db = get_db()
            groups = db.execute("SELECT * FROM groups").fetchall()
            infos = []
            
            # Sync Equipment
            global_total_cost = 0
            for grp in groups:
                g_name = slugify(grp['name'])
                equips = db.execute("SELECT * FROM equipment WHERE group_id = ?", (grp['id'],)).fetchall()
                equip_slugs = get_unique_slugs(equips)
                loc_total_cost = 0
                
                for e in equips:
                    e_slug = e['mqtt_slug']
                    e_name = e['display_name']
                    unique_id = f"supplies_{g_name}_{e_slug}"
                    if e['is_mqtt_enabled'] == 0:
                        infos.append(client.publish(f"homeassistant/sensor/{unique_id}/config", "", retain=True))
                        continue
                        
                    metrics = get_metrics(e, db)
                    safe_cost = metrics['cost'] if metrics['cost'] is not None else 0
                    loc_total_cost += safe_cost 
                    
                    infos.append(client.publish(f"homeassistant/sensor/{unique_id}/config", json.dumps({
                        "name": e_name, "unique_id": unique_id, "object_id": unique_id,
                        "state_topic": f"supplies/{g_name}/{e_slug}/state",
                        "value_template": "{{ value_json.percent }}", "unit_of_measurement": "%",
                        "icon": "mdi:water-filter", "json_attributes_topic": f"supplies/{g_name}/{e_slug}/state",
                        "device": {"identifiers": [f"supplies_{g_name}"], "name": f"Vật tư: {grp['name']}", "manufacturer": "Mini App"}
                    }), retain=True))
                    infos.append(client.publish(f"supplies/{g_name}/{e_slug}/state", json.dumps({
                        "percent": metrics['percent'], "days_left": metrics['days_left'], "days_used": metrics['days_used'],
                        "next_date": metrics['next_date'], "last_date": metrics['replace_date'], "cost": safe_cost,
                        "need_replace": 1 if metrics['need_replace'] else 0, "timestamp": datetime.now(VN_TZ).isoformat()
                    }), retain=True))
                
                global_total_cost += loc_total_cost
                loc_cost_id = f"supplies_cost_{g_name}"
                infos.append(client.publish(f"homeassistant/sensor/{loc_cost_id}/config", json.dumps({
                    "name": "Tổng chi phí", "unique_id": loc_cost_id, "object_id": loc_cost_id, "state_topic": f"supplies/{g_name}/total_cost",
                    "unit_of_measurement": "VNĐ", "icon": "mdi:currency-vnd",
                    "device": {"identifiers": [f"supplies_{g_name}"], "name": f"Vật tư: {grp['name']}", "manufacturer": "Mini App"}
                }), retain=True))
                infos.append(client.publish(f"supplies/{g_name}/total_cost", str(loc_total_cost), retain=True))

            infos.append(client.publish(f"homeassistant/sensor/supplies_global_total_cost/config", json.dumps({
                "name": "Tổng chi phí vật tư", "unique_id": "supplies_global_total_cost", "object_id": "supplies_global_total_cost", "state_topic": "supplies/global/total_cost",
                "unit_of_measurement": "VNĐ", "icon": "mdi:cash-multiple"
            }), retain=True))
            infos.append(client.publish("supplies/global/total_cost", str(global_total_cost), retain=True))
            
            # Sync Vehicles
            vehicles = db.execute("SELECT * FROM vehicles").fetchall()
            for veh in vehicles:
                v_slug = veh['mqtt_slug']
                v_name = veh['display_name']
                parts = db.execute("SELECT * FROM parts WHERE vehicle_id = ?", (veh['id'],)).fetchall()
                if veh['is_mqtt_enabled'] == 0:
                    infos.append(client.publish(f"homeassistant/sensor/vehicle_{v_slug}_odo/config", "", retain=True))
                    infos.append(client.publish(f"homeassistant/sensor/vehicle_{v_slug}_avg_km/config", "", retain=True))
                    infos.append(client.publish(f"homeassistant/sensor/vehicle_{v_slug}_cost/config", "", retain=True))
                    for p in parts:
                        infos.append(client.publish(f"homeassistant/sensor/veh_{v_slug}_{p['mqtt_slug']}/config", "", retain=True))
                    continue

                stats = get_vehicle_stats(veh['id'], db)
                veh_total_cost = 0
                
                infos.append(client.publish(f"homeassistant/sensor/vehicle_{v_slug}_odo/config", json.dumps({
                    "name": "ODO", "unique_id": f"veh_{v_slug}_odo", "object_id": f"xe_{v_slug}_odo", "state_topic": f"vehicle/{v_slug}/odo/state", 
                    "unit_of_measurement": "km", "icon": "mdi:speedometer", "device": {"identifiers": [f"veh_{v_slug}"], "name": f"Xe: {v_name}", "manufacturer": "Mini App"}
                }), retain=True))
                infos.append(client.publish(f"vehicle/{v_slug}/odo/state", str(stats['current_odo']), retain=True))

                infos.append(client.publish(f"homeassistant/sensor/vehicle_{v_slug}_avg_km/config", json.dumps({
                    "name": "Quãng đường trung bình", "unique_id": f"veh_{v_slug}_avg_km", "object_id": f"xe_{v_slug}_quang_duong_trung_binh", "state_topic": f"vehicle/{v_slug}/avg_km/state", 
                    "unit_of_measurement": "km/ngày", "icon": "mdi:chart-line-variant", "device": {"identifiers": [f"veh_{v_slug}"], "name": f"Xe: {v_name}", "manufacturer": "Mini App"}
                }), retain=True))
                infos.append(client.publish(f"vehicle/{v_slug}/avg_km/state", str(stats['avg_km_day']), retain=True))

                for p in parts:
                    p_slug = p['mqtt_slug']
                    p_name = p['display_name']
                    unique_id = f"veh_{v_slug}_{p_slug}"
                    if p['is_mqtt_enabled'] == 0:
                        infos.append(client.publish(f"homeassistant/sensor/{unique_id}/config", "", retain=True))
                        continue
                    
                    metrics = calculate_part_metrics(p, stats['current_odo'], stats['avg_km_day'], db)
                    veh_total_cost += metrics['cost']
                    infos.append(client.publish(f"homeassistant/sensor/{unique_id}/config", json.dumps({
                        "name": p_name, "unique_id": unique_id, "object_id": f"xe_{v_slug}_{p_slug}", "state_topic": f"vehicle/{v_slug}/{p_slug}/state", 
                        "value_template": "{{ value_json.percent }}", "unit_of_measurement": "%", "icon": "mdi:engine", "json_attributes_topic": f"vehicle/{v_slug}/{p_slug}/state",
                        "device": {"identifiers": [f"veh_{v_slug}"], "name": f"Xe: {v_name}", "manufacturer": "Mini App"}
                    }), retain=True))
                    infos.append(client.publish(f"vehicle/{v_slug}/{p_slug}/state", json.dumps({
                        "percent": metrics['percent'], "km_left": metrics['km_left'], "km_used": metrics['km_used'],
                        "days_left": metrics['days_left'], "next_date": metrics['next_date'], "last_date": metrics['replace_date'],
                        "total_cost": metrics['cost'], "replace_odo": metrics['replace_odo'], "timestamp": datetime.now(VN_TZ).isoformat()
                    }), retain=True))
                
                infos.append(client.publish(f"homeassistant/sensor/vehicle_{v_slug}_cost/config", json.dumps({
                    "name": "Chi phí bảo dưỡng", "unique_id": f"veh_{v_slug}_cost", "object_id": f"xe_{v_slug}_chi_phi_bao_duong", "state_topic": f"vehicle/{v_slug}/cost/state", 
                    "unit_of_measurement": "VNĐ", "icon": "mdi:cash-multiple", "device": {"identifiers": [f"veh_{v_slug}"], "name": f"Xe: {v_name}", "manufacturer": "Mini App"}
                }), retain=True))
                infos.append(client.publish(f"vehicle/{v_slug}/cost/state", str(veh_total_cost), retain=True))

            db.close()
            
        for i in infos: i.wait_for_publish(timeout=0.5)
        client.loop_stop()
        client.disconnect()
    except Exception as e: 
        print(f"Lỗi MQTT Sync: {e}")
        global mqtt_status
        mqtt_status = f"Error: {e}"

def unpublish_mqtt_entities(topics: list):
    """Gửi empty payload để xóa thực thể khỏi Home Assistant"""
    try:
        client = mqtt.Client()
        if os.getenv("MQTT_USER"): client.username_pw_set(os.getenv("MQTT_USER"), os.getenv("MQTT_PASSWORD"))
        client.connect(os.getenv("MQTT_BROKER", "localhost"), int(os.getenv("MQTT_PORT", 1883)), 60)
        client.loop_start()
        import time; time.sleep(0.3)  # Chờ kết nối
        for topic in topics:
            client.publish(topic, "", retain=True)
        import time; time.sleep(0.5)
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        print(f"Lỗi unpublish MQTT: {e}")

def start_mqtt_listener():
    client = mqtt.Client()
    
    def on_connect_listener(c, userdata, flags, rc):
        on_connect(c, userdata, flags, rc)
        if rc == 0:
            c.subscribe("vehicle/update/odo/#")

    client.on_connect = on_connect_listener
    client.on_disconnect = on_disconnect
    if os.getenv("MQTT_USER"): client.username_pw_set(os.getenv("MQTT_USER"), os.getenv("MQTT_PASSWORD"))
    
    def on_message(c, u, msg):
        try:
            topic_parts = msg.topic.split('/')
            if len(topic_parts) >= 4 and topic_parts[1] == "update":
                vehicle_slug = topic_parts[3] 
                new_odo = int(float(msg.payload.decode()))
                updated_veh = None
                with db_lock:
                    db = get_db()
                    vehicles = db.execute("SELECT * FROM vehicles").fetchall()
                    for v in vehicles:
                        if v['mqtt_slug'] == vehicle_slug or slugify(v['name']) == vehicle_slug:
                            today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
                            cursor = db.cursor()
                            cursor.execute("UPDATE odo_logs SET odo = ? WHERE vehicle_id = ? AND log_date = ?", (new_odo, v['id'], today))
                            if cursor.rowcount == 0:
                                cursor.execute("INSERT INTO odo_logs (vehicle_id, odo, log_date) VALUES (?, ?, ?)", (v['id'], new_odo, today))
                            db.commit()
                            log_audit("MQTT Receive", f"Hass cập nhật ODO cho {v['display_name']}: {new_odo}")
                            updated_veh = v
                            break
                    
                    if updated_veh and updated_veh['is_mqtt_enabled'] == 1:
                        v_slug = updated_veh['mqtt_slug']
                        stats = get_vehicle_stats(updated_veh['id'], db)
                        c.publish(f"vehicle/{v_slug}/odo/state", str(stats['current_odo']), retain=True)
                        c.publish(f"vehicle/{v_slug}/avg_km/state", str(stats['avg_km_day']), retain=True)
                        
                        parts = db.execute("SELECT * FROM parts WHERE vehicle_id = ?", (updated_veh['id'],)).fetchall()
                        veh_total_cost = 0
                        for p in parts:
                            if p['is_mqtt_enabled'] == 0: continue
                            p_slug = p['mqtt_slug']
                            metrics = calculate_part_metrics(p, stats['current_odo'], stats['avg_km_day'], db)
                            veh_total_cost += metrics['cost']
                            c.publish(f"vehicle/{v_slug}/{p_slug}/state", json.dumps({
                                "percent": metrics['percent'], "km_left": metrics['km_left'], "km_used": metrics['km_used'],
                                "days_left": metrics['days_left'], "next_date": metrics['next_date'], "last_date": metrics['replace_date'],
                                "total_cost": metrics['cost'], "replace_odo": metrics['replace_odo'], "timestamp": datetime.now(VN_TZ).isoformat()
                            }), retain=True)
                        c.publish(f"vehicle/{v_slug}/cost/state", str(veh_total_cost), retain=True)
                    db.close()
                
                # Gọi đồng bộ file CSV bằng luồng nền thay vì block tiến trình nhận MQTT
                import threading
                threading.Thread(target=export_csv_task, daemon=True).start()
                
        except Exception as e: log_audit("MQTT Error", str(e))

    client.on_message = on_message
    try:
        client.connect(os.getenv("MQTT_BROKER", "localhost"), int(os.getenv("MQTT_PORT", 1883)), 60)
        client.loop_forever()
    except Exception as e:
        print("Không thể khởi động MQTT listener", e)

# ================= ROUTES =================
@app.get("/api/sys-status")
def sys_status():
    return JSONResponse({
        "time": datetime.now(VN_TZ).strftime("%d/%m/%Y %H:%M:%S"),
        "mqtt_status": mqtt_status
    })

@app.get("/api/logs")
def get_logs(page: int = 1):
    limit = 100
    offset = (page - 1) * limit
    with db_lock:
        db = get_db()
        total_logs = db.execute("SELECT COUNT(*) as c FROM audit_logs").fetchone()['c']
        logs = db.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        db.close()
    
    total_pages = (total_logs + limit - 1) // limit if total_logs > 0 else 1
    return JSONResponse({
        "logs": [dict(l) for l in logs],
        "current_page": page,
        "total_pages": total_pages
    })

@app.post("/upload-avatar")
async def upload_avatar(item_type: str = Form(...), item_id: int = Form(...), file: UploadFile = File(...)):
    file_loc = f"data/avatars/{item_type}_{item_id}_{file.filename}"
    with open(file_loc, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    avatar_url = f"/avatars/{item_type}_{item_id}_{file.filename}"
    with db_lock:
        db = get_db()
        if item_type == 'device':
            db.execute("UPDATE equipment SET avatar_url=? WHERE id=?", (avatar_url, item_id))
        elif item_type == 'part':
            db.execute("UPDATE parts SET avatar_url=? WHERE id=?", (avatar_url, item_id))
        elif item_type == 'vehicle':
            db.execute("UPDATE vehicles SET avatar_url=? WHERE id=?", (avatar_url, item_id))
        db.commit()
        db.close()
    log_audit("Upload Avatar", f"Đã cập nhật ảnh cho {item_type} ID: {item_id}")
    return RedirectResponse(url="/", status_code=303)

@app.post("/toggle-mqtt")
def toggle_mqtt(background_tasks: BackgroundTasks, item_type: str = Form(...), item_id: int = Form(...)):
    with db_lock:
        db = get_db()
        if item_type == 'device':
            curr = db.execute("SELECT is_mqtt_enabled FROM equipment WHERE id=?", (item_id,)).fetchone()
            db.execute("UPDATE equipment SET is_mqtt_enabled=? WHERE id=?", (0 if curr[0] else 1, item_id))
        elif item_type == 'part':
            curr = db.execute("SELECT is_mqtt_enabled FROM parts WHERE id=?", (item_id,)).fetchone()
            db.execute("UPDATE parts SET is_mqtt_enabled=? WHERE id=?", (0 if curr[0] else 1, item_id))
        elif item_type == 'vehicle':
            curr = db.execute("SELECT is_mqtt_enabled FROM vehicles WHERE id=?", (item_id,)).fetchone()
            db.execute("UPDATE vehicles SET is_mqtt_enabled=? WHERE id=?", (0 if curr[0] else 1, item_id))
        db.commit()
        db.close()
    log_audit("Toggle MQTT", f"Thay đổi trạng thái MQTT cho {item_type} ID: {item_id}")
    background_tasks.add_task(sync_mqtt_now)
    return RedirectResponse(url="/", status_code=303)

@app.post("/toggle-pin")
def toggle_pin(item_type: str = Form(...), item_id: int = Form(...)):
    with db_lock:
        db = get_db()
        if item_type == 'device':
            curr = db.execute("SELECT is_pinned FROM equipment WHERE id=?", (item_id,)).fetchone()
            db.execute("UPDATE equipment SET is_pinned=? WHERE id=?", (0 if curr[0] else 1, item_id))
        elif item_type == 'part':
            curr = db.execute("SELECT is_pinned FROM parts WHERE id=?", (item_id,)).fetchone()
            db.execute("UPDATE parts SET is_pinned=? WHERE id=?", (0 if curr[0] else 1, item_id))
        db.commit()
        db.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/update-sort")
async def update_sort(request: Request):
    data = await request.json()
    item_type = data.get("type")
    ids = data.get("ids", [])
    with db_lock:
        db = get_db()
        for idx, item_id in enumerate(ids):
            if item_type == 'device':
                db.execute("UPDATE equipment SET sort_order=? WHERE id=?", (idx, item_id))
            elif item_type == 'part':
                db.execute("UPDATE parts SET sort_order=? WHERE id=?", (idx, item_id))
            elif item_type == 'vehicle':
                db.execute("UPDATE vehicles SET sort_order=? WHERE id=?", (idx, item_id))
        db.commit()
        db.close()
    return JSONResponse({"status": "success"})

@app.get("/api/check-name")
def check_name(type: str, name: str, group_id: int = None, vehicle_id: int = None):
    name = name.strip()
    if not name: return JSONResponse({"exists": False})
    
    with db_lock:
        db = get_db()
        items = []
        if type == "vehicle":
            items = db.execute("SELECT * FROM vehicles").fetchall()
        elif type == "device" and group_id:
            items = db.execute("SELECT * FROM equipment WHERE group_id = ?", (group_id,)).fetchall()
        elif type == "part" and vehicle_id:
            items = db.execute("SELECT * FROM parts WHERE vehicle_id = ?", (vehicle_id,)).fetchall()
        elif type == "group":
            items = db.execute("SELECT * FROM groups").fetchall()
        db.close()
        
    base_slug = slugify(name)
    count = sum(1 for item in items if slugify(item['name']) == base_slug)
    
    if count > 0:
        return JSONResponse({"exists": True, "new_name": f"{name} ({count})"})
    return JSONResponse({"exists": False})

@app.get("/")
def index(request: Request, group_id: int = None):
    with db_lock:
        db = get_db()
        groups = db.execute("SELECT * FROM groups").fetchall()
        if not groups:
            db.execute("INSERT INTO groups (name) VALUES ('Nhà tôi')")
            db.commit()
            groups = db.execute("SELECT * FROM groups").fetchall()
        
        valid_ids = [g['id'] for g in groups]
        if group_id not in valid_ids: group_id = valid_ids[0]

        # Lấy Devices
        equips = db.execute("SELECT * FROM equipment WHERE group_id = ? ORDER BY is_pinned DESC, sort_order ASC, id DESC", (group_id,)).fetchall()
        enriched_equips = [{**dict(e), **get_metrics(e, db)} for e in equips]
        group_equip_cost = sum((e['cost'] or 0) for e in enriched_equips)
        
        # Lấy Vehicles & Parts
        vehicles = db.execute("SELECT * FROM vehicles WHERE group_id = ? ORDER BY is_pinned DESC, sort_order ASC, id DESC", (group_id,)).fetchall()
        enriched_vehicles = []
        for v in vehicles:
            stats = get_vehicle_stats(v['id'], db)
            parts = db.execute("SELECT * FROM parts WHERE vehicle_id = ? ORDER BY is_pinned DESC, sort_order ASC, id DESC", (v['id'],)).fetchall()
            enriched_parts = [{**dict(p), **calculate_part_metrics(p, stats['current_odo'], stats['avg_km_day'], db)} for p in parts]
            veh_cost = sum((p['cost'] or 0) for p in enriched_parts)
            enriched_vehicles.append({
                **dict(v),
                "stats": stats,
                "parts": enriched_parts,
                "total_cost": veh_cost
            })
            
        group_veh_cost = sum(v['total_cost'] for v in enriched_vehicles)
        db.close()
        
    backup_status = None
    if os.path.exists("data/backup_info.json"):
        try:
            with open("data/backup_info.json", "r", encoding="utf-8") as f:
                backup_status = json.load(f)
        except Exception:
            pass

    is_backup_complete = False
    try:
        dest_db = os.path.join(BACKUP_DIR, "app.db")
        dest_csv1 = os.path.join(BACKUP_DIR, "supplies_export.csv")
        dest_csv2 = os.path.join(BACKUP_DIR, "vehicles_export.csv")
        files_needed = [dest_db]
        if os.path.exists(CSV_SUPPLIES): files_needed.append(dest_csv1)
        if os.path.exists(CSV_VEHICLES): files_needed.append(dest_csv2)
        is_backup_complete = all(os.path.exists(f) and os.path.getsize(f) > 0 for f in files_needed)
    except:
        pass

    return templates.TemplateResponse(
        request=request, name="index.html",
        context={
            "request": request, 
            "groups": groups, 
            "current_group": group_id, 
            "equipment": enriched_equips,
            "group_equip_cost": group_equip_cost,
            "vehicles": enriched_vehicles,
            "group_veh_cost": group_veh_cost,
            "backup_status": backup_status,
            "is_backup_complete": is_backup_complete,
        }
    )

@app.post("/add-group")
def add_group(background_tasks: BackgroundTasks, name: str = Form(...)):
    name = name.strip()
    with db_lock:
        db = get_db()
        existing_groups = db.execute("SELECT * FROM groups").fetchall()
        unique_name = generate_unique_group_name(existing_groups, name)
        db.execute("INSERT INTO groups (name) VALUES (?)", (unique_name,))
        db.commit()
        new_id = db.execute("SELECT id FROM groups ORDER BY id DESC LIMIT 1").fetchone()['id']
        db.close()
    log_audit("Add Group", f"Đã thêm nhóm mới: {unique_name}")
    msg = f"Đã thêm thành công nhóm '{unique_name}'"
    background_tasks.add_task(sync_mqtt_now)
    return RedirectResponse(url=f"/?group_id={new_id}&success={urllib.parse.quote(msg)}", status_code=303)

@app.post("/delete-group")
def delete_group(group_id: int = Form(...)):
    with db_lock:
        db = get_db()
        groups_to_delete = db.execute("SELECT name FROM groups WHERE id = ?", (group_id,)).fetchone()
        if groups_to_delete:
            g_slug = slugify(groups_to_delete['name'])
            topics = []
            
            equips = db.execute("SELECT mqtt_slug FROM equipment WHERE group_id = ?", (group_id,)).fetchall()
            for e in equips:
                topics.append(f"homeassistant/sensor/supplies_{g_slug}_{e['mqtt_slug']}/config")
                topics.append(f"supplies/{g_slug}/{e['mqtt_slug']}/state")
            topics.append(f"homeassistant/sensor/supplies_cost_{g_slug}/config")
            topics.append(f"supplies/{g_slug}/total_cost")
            
            vehicles = db.execute("SELECT id, mqtt_slug FROM vehicles WHERE group_id = ?", (group_id,)).fetchall()
            for v in vehicles:
                v_slug = v['mqtt_slug']
                topics.append(f"homeassistant/sensor/vehicle_{v_slug}_odo/config")
                topics.append(f"homeassistant/sensor/vehicle_{v_slug}_avg_km/config")
                topics.append(f"homeassistant/sensor/vehicle_{v_slug}_cost/config")
                topics.append(f"vehicle/{v_slug}/odo/state")
                topics.append(f"vehicle/{v_slug}/avg_km/state")
                topics.append(f"vehicle/{v_slug}/cost/state")
                
                parts = db.execute("SELECT mqtt_slug FROM parts WHERE vehicle_id = ?", (v['id'],)).fetchall()
                for p in parts:
                    topics.append(f"homeassistant/sensor/veh_{v_slug}_{p['mqtt_slug']}/config")
                    topics.append(f"vehicle/{v_slug}/{p['mqtt_slug']}/state")
            
            unpublish_mqtt_entities(topics)

        db.execute("DELETE FROM history WHERE equipment_id IN (SELECT id FROM equipment WHERE group_id = ?)", (group_id,))
        db.execute("DELETE FROM equipment WHERE group_id = ?", (group_id,))
        db.execute("DELETE FROM part_history WHERE part_id IN (SELECT id FROM parts WHERE vehicle_id IN (SELECT id FROM vehicles WHERE group_id = ?))", (group_id,))
        db.execute("DELETE FROM parts WHERE vehicle_id IN (SELECT id FROM vehicles WHERE group_id = ?)", (group_id,))
        db.execute("DELETE FROM odo_logs WHERE vehicle_id IN (SELECT id FROM vehicles WHERE group_id = ?)", (group_id,))
        db.execute("DELETE FROM vehicles WHERE group_id = ?", (group_id,))
        db.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        db.commit()
        db.close()
    log_audit("Delete Group", f"Đã xoá nhóm ID {group_id} và các thiết bị bên trong")
    return RedirectResponse(url="/", status_code=303)

@app.post("/add-item")
def add_item(background_tasks: BackgroundTasks, item_type: str = Form(...), group_id: int = Form(...), name: str = Form(...),
             lifetime: int = Form(0), purchase_link: str = Form(""), notes: str = Form(""), vehicle_id: int = Form(None),
             is_mqtt_enabled: str = Form("off")):
    mqtt_val = 1 if is_mqtt_enabled == "on" else 0
    name = name.strip()
    with db_lock:
        db = get_db()
        if item_type == "device":
            existing = db.execute("SELECT * FROM equipment WHERE group_id = ?", (group_id,)).fetchall()
            p_slug, d_name = generate_permanent_slug_and_name(existing, name)
            db.execute("INSERT INTO equipment (name, mqtt_slug, display_name, purchase_link, lifetime_days, notes, group_id, is_mqtt_enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                       (name, p_slug, d_name, purchase_link, lifetime, notes, group_id, mqtt_val))
            log_audit("Add Device", f"Thêm thiết bị {d_name} vào nhóm {group_id}")
            display_name = d_name
        elif item_type == "vehicle":
            existing = db.execute("SELECT * FROM vehicles").fetchall()
            p_slug, d_name = generate_permanent_slug_and_name(existing, name)
            db.execute("INSERT INTO vehicles (name, mqtt_slug, display_name, group_id, is_mqtt_enabled) VALUES (?, ?, ?, ?, ?)", 
                       (name, p_slug, d_name, group_id, mqtt_val))
            log_audit("Add Vehicle", f"Thêm xe {d_name} vào nhóm {group_id}")
            display_name = d_name
        elif item_type == "part" and vehicle_id:
            existing = db.execute("SELECT * FROM parts WHERE vehicle_id = ?", (vehicle_id,)).fetchall()
            p_slug, d_name = generate_permanent_slug_and_name(existing, name)
            db.execute("INSERT INTO parts (name, mqtt_slug, display_name, purchase_link, lifetime_km, notes, vehicle_id, is_mqtt_enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                       (name, p_slug, d_name, purchase_link, lifetime, notes, vehicle_id, mqtt_val))
            log_audit("Add Part", f"Thêm phụ tùng {d_name} cho xe {vehicle_id}")
            display_name = d_name
        db.commit()
            
        db.close()
    background_tasks.add_task(sync_mqtt_now)
    background_tasks.add_task(export_csv_task)
    
    msg = f"Đã thêm thành công '{display_name}'"
    return RedirectResponse(url=f"/?group_id={group_id}&success={urllib.parse.quote(msg)}", status_code=303)

@app.post("/update-equipment-details")
def update_equipment_details(background_tasks: BackgroundTasks, equip_id: int = Form(...), group_id: int = Form(...),
                             lifetime_days: int = Form(...), purchase_link: str = Form(""), notes: str = Form(""),
                             replace_date: str = Form(""), cost: float = Form(0)):
    with db_lock:
        db = get_db()
        db.execute("UPDATE equipment SET lifetime_days=?, purchase_link=?, notes=? WHERE id=?", (lifetime_days, purchase_link, notes, equip_id))
        if replace_date:
            exist_hist = db.execute("SELECT id FROM history WHERE equipment_id = ? ORDER BY id DESC LIMIT 1", (equip_id,)).fetchone()
            if exist_hist: db.execute("UPDATE history SET replace_date=?, cost=? WHERE id=?", (replace_date, cost, exist_hist['id']))
            else: db.execute("INSERT INTO history (equipment_id, replace_date, cost) VALUES (?, ?, ?)", (equip_id, replace_date, cost))
        db.commit()
        db.close()
    log_audit("Update Device", f"Cập nhật thiết bị ID {equip_id}")
    background_tasks.add_task(sync_mqtt_now)
    background_tasks.add_task(export_csv_task)
    return RedirectResponse(url=f"/?group_id={group_id}", status_code=303)

@app.post("/replace-equipment")
def replace_equipment(background_tasks: BackgroundTasks, equip_id: int = Form(...), group_id: int = Form(...), cost: float = Form(0)):
    with db_lock:
        db = get_db()
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        db.execute("INSERT INTO history (equipment_id, replace_date, cost) VALUES (?, ?, ?)", (equip_id, today, cost))
        db.commit(); db.close()
    log_audit("Replace Device", f"Thay mới thiết bị ID {equip_id}")
    background_tasks.add_task(sync_mqtt_now)
    background_tasks.add_task(export_csv_task)
    return RedirectResponse(url=f"/?group_id={group_id}", status_code=303)

@app.post("/delete-equipment")
def delete_equipment(background_tasks: BackgroundTasks, equip_id: int = Form(...), group_id: int = Form(...)):
    with db_lock:
        db = get_db()
        equip = db.execute("SELECT e.*, g.name as gname FROM equipment e JOIN groups g ON e.group_id = g.id WHERE e.id = ?", (equip_id,)).fetchone()
        if equip:
            g_slug = slugify(equip['gname'])
            e_slug = equip['mqtt_slug']
            unique_id = f"supplies_{g_slug}_{e_slug}"
            # Xóa thực thể khỏi Hass trước
            unpublish_mqtt_entities([
                f"homeassistant/sensor/{unique_id}/config",
                f"supplies/{g_slug}/{e_slug}/state",
            ])
        db.execute("DELETE FROM history WHERE equipment_id = ?", (equip_id,))
        db.execute("DELETE FROM equipment WHERE id = ?", (equip_id,))
        db.commit()
        db.close()
    log_audit("Delete Device", f"Xóa thiết bị ID {equip_id}")
    background_tasks.add_task(sync_mqtt_now)
    background_tasks.add_task(export_csv_task)
    return RedirectResponse(url=f"/?group_id={group_id}", status_code=303)

# ---- VEHICLE ROUTES ----
@app.post("/update-odo")
def update_odo(background_tasks: BackgroundTasks, vehicle_id: int = Form(...), group_id: int = Form(...), new_odo: str = Form(...)):
    new_odo_val = int(float(new_odo))
    with db_lock:
        db = get_db()
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        cursor = db.cursor()
        cursor.execute("UPDATE odo_logs SET odo = ? WHERE vehicle_id = ? AND log_date = ?", (new_odo_val, vehicle_id, today))
        if cursor.rowcount == 0: cursor.execute("INSERT INTO odo_logs (vehicle_id, odo, log_date) VALUES (?, ?, ?)", (vehicle_id, new_odo_val, today))
        db.commit()
        db.close()
    log_audit("Update ODO", f"Cập nhật ODO xe ID {vehicle_id} thành {new_odo_val}")
    background_tasks.add_task(sync_mqtt_now)
    background_tasks.add_task(export_csv_task)
    return RedirectResponse(url=f"/?group_id={group_id}", status_code=303)

@app.post("/replace-part")
def replace_part(background_tasks: BackgroundTasks, part_id: int = Form(...), vehicle_id: int = Form(...), group_id: int = Form(...), cost: str = Form("0")):
    cost_val = float(cost) if cost.strip() else 0.0
    with db_lock:
        db = get_db()
        stats = get_vehicle_stats(vehicle_id, db)
        today = datetime.now(VN_TZ).strftime("%Y-%m-%d")
        db.execute("DELETE FROM part_history WHERE part_id = ?", (part_id,))
        db.execute("INSERT INTO part_history (part_id, replace_odo, replace_date, cost) VALUES (?, ?, ?, ?)", 
                   (part_id, stats['current_odo'], today, cost_val))
        db.commit(); db.close()
    log_audit("Replace Part", f"Thay mới phụ tùng ID {part_id}")
    background_tasks.add_task(sync_mqtt_now)
    background_tasks.add_task(export_csv_task)
    return RedirectResponse(url=f"/?group_id={group_id}", status_code=303)

@app.post("/update-part-details")
def update_part_details(background_tasks: BackgroundTasks, part_id: int = Form(...), vehicle_id: int = Form(...), group_id: int = Form(...),
                        lifetime_km: str = Form(...), purchase_link: str = Form(""), notes: str = Form(""),
                        replace_date: str = Form(""), replace_odo: str = Form(...), km_left: str = Form(""), cost: str = Form("0")):
    lifetime_km_val = int(float(lifetime_km))
    replace_odo_val = int(float(replace_odo))
    cost_val = float(cost) if cost.strip() else 0.0
    with db_lock:
        db = get_db()
        stats = get_vehicle_stats(vehicle_id, db)
        km_used = max(0, stats['current_odo'] - replace_odo_val)
        if km_left.strip():
            try: lifetime_km_val = km_used + int(float(km_left))
            except ValueError: pass
        db.execute("UPDATE parts SET lifetime_km=?, purchase_link=?, notes=? WHERE id=?", (lifetime_km_val, purchase_link, notes, part_id))
        parsed_rd = parse_date_to_iso(replace_date)
        replace_date_iso = parsed_rd.strftime("%Y-%m-%d") if parsed_rd else replace_date
        exist_hist = db.execute("SELECT id FROM part_history WHERE part_id = ?", (part_id,)).fetchone()
        if exist_hist: db.execute("UPDATE part_history SET replace_date=?, replace_odo=?, cost=? WHERE id=?", (replace_date_iso, replace_odo_val, cost_val, exist_hist['id']))
        else: db.execute("INSERT INTO part_history (part_id, replace_date, replace_odo, cost) VALUES (?, ?, ?, ?)", (part_id, replace_date_iso, replace_odo_val, cost_val))
        db.commit(); db.close()
    log_audit("Update Part", f"Cập nhật phụ tùng ID {part_id}")
    background_tasks.add_task(sync_mqtt_now)
    background_tasks.add_task(export_csv_task)
    return RedirectResponse(url=f"/?group_id={group_id}", status_code=303)

@app.post("/delete-part")
def delete_part(background_tasks: BackgroundTasks, part_id: int = Form(...), group_id: int = Form(...)):
    with db_lock:
        db = get_db()
        part = db.execute("SELECT p.*, v.mqtt_slug as v_slug FROM parts p JOIN vehicles v ON p.vehicle_id = v.id WHERE p.id = ?", (part_id,)).fetchone()
        if part:
            v_slug = part['v_slug']
            p_slug = part['mqtt_slug']
            unpublish_mqtt_entities([
                f"homeassistant/sensor/veh_{v_slug}_{p_slug}/config",
                f"vehicle/{v_slug}/{p_slug}/state"
            ])
            
        db.execute("DELETE FROM part_history WHERE part_id = ?", (part_id,))
        db.execute("DELETE FROM parts WHERE id = ?", (part_id,))
        db.commit()
        db.close()
    log_audit("Delete Part", f"Xóa phụ tùng ID {part_id}")
    background_tasks.add_task(sync_mqtt_now)
    background_tasks.add_task(export_csv_task)
    return RedirectResponse(url=f"/?group_id={group_id}", status_code=303)

@app.post("/delete-vehicle")
def delete_vehicle(background_tasks: BackgroundTasks, vehicle_id: int = Form(...), group_id: int = Form(...)):
    with db_lock:
        db = get_db()
        veh = db.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
        if veh:
            v_slug = veh['mqtt_slug']
            parts = db.execute("SELECT mqtt_slug FROM parts WHERE vehicle_id = ?", (vehicle_id,)).fetchall()
            
            topics = [
                f"homeassistant/sensor/vehicle_{v_slug}_odo/config",
                f"homeassistant/sensor/vehicle_{v_slug}_avg_km/config",
                f"homeassistant/sensor/vehicle_{v_slug}_cost/config",
                f"vehicle/{v_slug}/odo/state",
                f"vehicle/{v_slug}/avg_km/state",
                f"vehicle/{v_slug}/cost/state",
            ]
            for p in parts:
                p_slug = p['mqtt_slug']
                topics.append(f"homeassistant/sensor/veh_{v_slug}_{p_slug}/config")
                topics.append(f"vehicle/{v_slug}/{p_slug}/state")
            # Xóa thực thể khỏi Hass trước
            unpublish_mqtt_entities(topics)
        db.execute("DELETE FROM part_history WHERE part_id IN (SELECT id FROM parts WHERE vehicle_id = ?)", (vehicle_id,))
        db.execute("DELETE FROM parts WHERE vehicle_id = ?", (vehicle_id,))
        db.execute("DELETE FROM odo_logs WHERE vehicle_id = ?", (vehicle_id,))
        db.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        db.commit()
        db.close()
    log_audit("Delete Vehicle", f"Xóa xe ID {vehicle_id}")
    background_tasks.add_task(sync_mqtt_now)
    background_tasks.add_task(export_csv_task)
    return RedirectResponse(url=f"/?group_id={group_id}", status_code=303)

async def mqtt_loop():
    loop = asyncio.get_event_loop()
    while True:
        await loop.run_in_executor(None, sync_mqtt_now)
        await asyncio.sleep(600)

BACKUP_DIR = "/backup"
BACKUP_INFO_FILE = "data/backup_info.json"

def perform_backup(is_manual=False):
    status = {"success": False, "time": datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S"), "message": ""}
    try:
        if not os.path.exists(BACKUP_DIR):
            os.makedirs(BACKUP_DIR, exist_ok=True)
            
        # Kiểm tra ghi thử để đảm bảo ổ cứng HDD đã mount và có quyền ghi (chống sao lưu ảo)
        test_file = os.path.join(BACKUP_DIR, ".test_write")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)

        # Cập nhật CSV mới nhất trước khi backup
        export_csv_task()
            
        dest_db = os.path.join(BACKUP_DIR, "app.db")
        dest_csv1 = os.path.join(BACKUP_DIR, "supplies_export.csv")
        dest_csv2 = os.path.join(BACKUP_DIR, "vehicles_export.csv")
        
        with db_lock:
            shutil.copy2(DB_PATH, dest_db)
            if os.path.exists(CSV_SUPPLIES):
                shutil.copy2(CSV_SUPPLIES, dest_csv1)
            if os.path.exists(CSV_VEHICLES):
                shutil.copy2(CSV_VEHICLES, dest_csv2)
                
        # Kiểm tra xác minh file đã thực sự được sao lưu đủ 3 file và dung lượng khớp với bản gốc
        files_to_check = [(DB_PATH, dest_db)]
        if os.path.exists(CSV_SUPPLIES): files_to_check.append((CSV_SUPPLIES, dest_csv1))
        if os.path.exists(CSV_VEHICLES): files_to_check.append((CSV_VEHICLES, dest_csv2))

        for src, dst in files_to_check:
            if not os.path.exists(dst):
                raise Exception(f"File {os.path.basename(dst)} không tồn tại ở đích!")
            src_size = os.path.getsize(src)
            dst_size = os.path.getsize(dst)
            if src_size != dst_size or dst_size == 0:
                raise Exception(f"Lỗi ghi {os.path.basename(dst)} (Size {dst_size}/{src_size}). Ổ cứng có thể bị đầy, mount lỗi hoặc hỏng hóc!")
            
        status["success"] = True
        status["message"] = "Đã kiểm tra và sao lưu đủ 3 file DB & CSV"
        log_audit("Backup", f"Sao lưu {'thủ công' if is_manual else 'tự động'} thành công.")
    except Exception as e:
        status["success"] = False
        status["message"] = str(e)
        log_audit("Backup", f"Lỗi sao lưu: {e}")
        
    with open(BACKUP_INFO_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False)
        
    return status

@app.post("/trigger-backup")
def trigger_backup():
    status = perform_backup(is_manual=True)
    return JSONResponse(status)

async def scheduled_backup_loop():
    while True:
        now = datetime.now(VN_TZ)
        # Nếu là Chủ nhật (weekday() == 6) và giờ là 00:00
        if now.weekday() == 6 and now.hour == 0 and now.minute == 0:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, perform_backup, False)
            # Chờ 60s để tránh chạy lặp lại trong cùng 1 phút
            await asyncio.sleep(60)
        else:
            # Kiểm tra mỗi 30s
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    export_csv_task()
    log_audit("System", "App Khởi Động")
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, start_mqtt_listener) 
    asyncio.create_task(mqtt_loop())
    asyncio.create_task(scheduled_backup_loop())
