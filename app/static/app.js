document.addEventListener('htmx:configRequest', function (event) {
  const token = document.querySelector('meta[name="csrf-token"]');
  if (token) event.detail.headers['X-CSRF-Token'] = token.content;
});
document.addEventListener('htmx:beforeSwap', function (event) {
  if (event.detail.xhr.status >= 400) {
    event.detail.target = document.getElementById('errors');
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
});
document.addEventListener('htmx:sendError', function () {
  const target = document.getElementById('errors');
  if (target) target.textContent = 'Connection lost. Please try again.';
});
