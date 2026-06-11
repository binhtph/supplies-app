/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
  ],
  theme: {
    extend: {
      fontFamily: { sans: ['-apple-system', 'BlinkMacSystemFont', '"SF Pro Display"', 'Roboto', 'sans-serif'] },
      colors: {
        ios: { bg: '#F2F2F7', card: '#FFFFFF', blue: '#007AFF', green: '#34C759', orange: '#FF9500', red: '#FF3B30', gray: '#8E8E93', grayLight: '#E5E5EA', input: '#7676801F' }
      }
    }
  },
  plugins: [],
}
