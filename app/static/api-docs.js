window.addEventListener('DOMContentLoaded', function () {
  SwaggerUIBundle({
    url: '/openapi.json', dom_id: '#swagger-ui', deepLinking: true, validatorUrl: null,
    requestInterceptor: async function (request) {
      if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method.toUpperCase())) {
        const path = new URL(request.url, window.location.href).pathname;
        if (path === '/api/v1/auth/login' || path === '/api/v1/auth/register') {
          const response = await fetch('/api/v1/auth/csrf', { credentials: 'same-origin' });
          request.headers['X-CSRF-Token'] = (await response.json()).csrf_token;
        } else {
          const cookie = document.cookie.split('; ').find(value => value.startsWith('hookdesk_csrf='));
          if (cookie) request.headers['X-CSRF-Token'] = decodeURIComponent(cookie.split('=')[1]);
        }
      }
      return request;
    }
  });
});
