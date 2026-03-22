/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
  ],
  theme: {
    extend: {
      colors: {
        amazon: '#FF9900',
        'amazon-dark': '#232F3E',
      }
    },
  },
  plugins: [],
}
