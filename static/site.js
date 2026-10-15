// Progressive enhancement: the manuscript and all navigation work without JS.
const search = document.querySelector('#chapter-search');
if (search) {
  const cards = [...document.querySelectorAll('.chapter-card')];
  const empty = document.querySelector('#search-empty');
  search.addEventListener('input', () => {
    const q = search.value.toLocaleLowerCase('ru').trim();
    cards.forEach(card => { card.hidden = !card.textContent.toLocaleLowerCase('ru').includes(q); });
    empty.hidden = cards.some(card => !card.hidden);
  });
}
if (matchMedia('(max-width: 800px)').matches) {
  const nav = document.querySelector('.nav-details');
  if (nav) nav.open = false;
}
const headings = [...document.querySelectorAll('main .manuscript-section')];
if ('IntersectionObserver' in window && headings.length) {
  const observer = new IntersectionObserver(entries => {
    entries.filter(e => e.isIntersecting).forEach(e => {
      document.querySelectorAll('.local-toc a').forEach(a => {
        if (a.hash === '#' + e.target.id) a.setAttribute('aria-current', 'location');
        else a.removeAttribute('aria-current');
      });
    });
  }, {rootMargin: '-10% 0px -65% 0px'});
  headings.forEach(h => observer.observe(h));
}
