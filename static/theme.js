// Переключение светлой/тёмной темы. Подключается в <head> каждой страницы.
(function () {
    function getSaved() {
        try { return localStorage.getItem('theme'); } catch (e) { return null; }
    }
    function save(theme) {
        try { localStorage.setItem('theme', theme); } catch (e) {}
    }

    // Применяем тему сразу, до отрисовки страницы, чтобы не было «мигания».
    var initial = getSaved() ||
        (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', initial);

    function updateButtons(theme) {
        document.querySelectorAll('.theme-toggle').forEach(function (btn) {
            var icon = btn.querySelector('.theme-icon');
            var label = btn.querySelector('.theme-label');
            if (icon) { icon.textContent = theme === 'dark' ? '☀' : '☾'; }
            else { btn.textContent = theme === 'dark' ? '☀️' : '🌙'; }
            if (label) { label.textContent = theme === 'dark' ? 'Светлая тема' : 'Тёмная тема'; }
            btn.setAttribute('aria-label', theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему');
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        updateButtons(document.documentElement.getAttribute('data-theme'));
        document.querySelectorAll('.theme-toggle').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var current = document.documentElement.getAttribute('data-theme');
                var next = current === 'dark' ? 'light' : 'dark';
                document.documentElement.setAttribute('data-theme', next);
                save(next);
                updateButtons(next);
            });
        });
    });
})();
