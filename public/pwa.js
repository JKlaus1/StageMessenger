// Makes each Stage Messenger page installable as a full-screen Android app (Chrome ⋮ → Install app).
// use-credentials: behind Cloudflare Access the manifest fetch must carry the Access cookie.
(function () {
  var h = document.head;
  var l = document.createElement('link');
  l.rel = 'manifest';
  l.crossOrigin = 'use-credentials';
  l.href = '/app.webmanifest?start=' + encodeURIComponent(location.pathname + location.search);
  h.appendChild(l);
  var t = document.createElement('meta');
  t.name = 'theme-color'; t.content = '#0d0d1a';
  h.appendChild(t);
})();
