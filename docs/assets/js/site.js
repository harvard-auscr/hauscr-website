/* hauscr.org static mirror - minimal vanilla header/mobile-nav behavior.
   Squarespace's runtime JS is stripped; this reproduces only what the header
   needs: burger toggle, folder drill-down, back control, and Escape-to-close.
   Desktop folder dropdowns are pure CSS :hover and need no JS. */
(function () {
  var body = document.body;
  var header = document.querySelector('.header');

  function openMenu() {
    body.classList.add('header--menu-open');
    body.classList.remove('header--menu-closed');
    if (header) header.classList.add('header--menu-open');
    resetFolders();
  }
  function closeMenu() {
    body.classList.remove('header--menu-open');
    body.classList.add('header--menu-closed');
    if (header) header.classList.remove('header--menu-open');
    resetFolders();
  }
  function isOpen() { return body.classList.contains('header--menu-open'); }

  function resetFolders() {
    var panes = document.querySelectorAll('.header-menu-nav-folder');
    panes.forEach(function (p) {
      if (p.getAttribute('data-folder') === 'root') p.classList.add('header-menu-nav-folder--active');
      else p.classList.remove('header-menu-nav-folder--active');
    });
  }
  function openFolder(id) {
    var pane = document.querySelector('.header-menu-nav-folder[data-folder="' + id + '"]');
    if (!pane) return;
    var root = document.querySelector('.header-menu-nav-folder[data-folder="root"]');
    if (root) root.classList.remove('header-menu-nav-folder--active');
    pane.classList.add('header-menu-nav-folder--active');
  }

  document.querySelectorAll('.header-burger-btn').forEach(function (b) {
    b.addEventListener('click', function (e) {
      e.preventDefault();
      isOpen() ? closeMenu() : openMenu();
    });
  });

  // Folder drill-down (mobile overlay): intercept folder-title links.
  document.querySelectorAll('.header-menu a[data-folder-id]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      openFolder(a.getAttribute('data-folder-id'));
    });
  });

  // Back controls return to the root pane.
  document.querySelectorAll('.header-menu [data-action="back"]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      resetFolders();
    });
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && isOpen()) closeMenu();
  });

  resetFolders();

  // Gallery reel arrows. The mirror lays each Squarespace 'gallery-reel' out as a
  // horizontal scroll strip (see mirror-overrides.css); the live site's runtime JS
  // slides one item per click, so do the same by scrolling one item width, and
  // wrap around at either end so the buttons are never dead.
  document.querySelectorAll('.gallery-reel').forEach(function (reel) {
    var list = reel.querySelector('.gallery-reel-list');
    if (!list) return;
    function step(dir) {
      var item = list.querySelector('.gallery-reel-item');
      var gap = 6;
      var w = item ? item.getBoundingClientRect().width + gap : list.clientWidth * 0.8;
      var max = Math.max(0, list.scrollWidth - list.clientWidth);
      var next = list.scrollLeft + dir * w;
      if (dir > 0 && list.scrollLeft >= max - 2) next = 0;
      else if (dir < 0 && list.scrollLeft <= 2) next = max;
      list.scrollTo({ left: Math.max(0, Math.min(max, next)), behavior: 'smooth' });
    }
    var scope = reel.closest('.gallery-reel-wrapper') || reel.parentElement || reel;
    scope.querySelectorAll('.gallery-reel-control-btn[data-previous]').forEach(function (b) {
      b.addEventListener('click', function (e) { e.preventDefault(); step(-1); });
    });
    scope.querySelectorAll('.gallery-reel-control-btn[data-next]').forEach(function (b) {
      b.addEventListener('click', function (e) { e.preventDefault(); step(1); });
    });
  });
})();
