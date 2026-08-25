/* Crosscut front end. Reads one static JSON produced by the build step. */

const LEANS = ['left', 'lean-left', 'center', 'lean-right', 'right'];
const LABEL = {
  'left': 'Left', 'lean-left': 'Lean left', 'center': 'Center',
  'lean-right': 'Lean right', 'right': 'Right',
};

let DATA = null;
let filter = 'all';
let query = '';

const $ = (s) => document.querySelector(s);

async function boot() {
  try {
    // Cache-bust so a fresh build shows up without a hard reload.
    const res = await fetch('data/stories.json?t=' + Math.floor(Date.now() / 60000));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    DATA = await res.json();
  } catch (err) {
    $('#main').innerHTML =
      '<p class="loading">Could not load coverage data (' + err.message + ').<br>' +
      'If this is a fresh deploy, the first scheduled build may not have run yet.</p>';
    return;
  }
  renderMeta();
  wire();
  render();
}

function ago(iso) {
  const mins = Math.round((Date.now() - new Date(iso)) / 60000);
  if (mins < 2) return 'just now';
  if (mins < 60) return mins + ' min ago';
  const h = Math.round(mins / 60);
  if (h < 24) return h + (h === 1 ? ' hour ago' : ' hours ago');
  const d = Math.round(h / 24);
  return d + (d === 1 ? ' day ago' : ' days ago');
}

function renderMeta() {
  const blind = DATA.stories.filter((s) => s.blindspot).length;
  $('#meta').innerHTML =
    '<b>' + DATA.article_count.toLocaleString() + '</b> articles from <b>' +
    DATA.outlet_count + '</b> outlets<br>' +
    '<b>' + DATA.story_count + '</b> stories · <b>' + blind + '</b> blindspots · updated ' + ago(DATA.generated);
  $('#outletTotal').textContent = DATA.outlets.length;

  if (DATA.failures && DATA.failures.length) {
    $('#health').innerHTML = '<b>' + DATA.failures.length + ' feed(s) failed this run:</b> ' +
      DATA.failures.map((f) => f.outlet).join(', ') +
      ' — those outlets are missing from the coverage below.';
  }
}

function wire() {
  document.querySelectorAll('#filters button').forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll('#filters button').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
      filter = b.dataset.filter;
      render();
    };
  });
  let t = null;
  $('#search').oninput = (e) => {
    clearTimeout(t);
    t = setTimeout(() => { query = e.target.value.trim().toLowerCase(); render(); }, 140);
  };
}

function visible() {
  return DATA.stories.filter((s) => {
    if (filter === 'blindspot' && !s.blindspot) return false;
    if (filter === 'wide' && s.outlets < 6) return false;
    if (query) {
      const hay = (s.title + ' ' + s.articles.map((a) => a.title + ' ' + a.outlet).join(' ')).toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  });
}

function covBar(leans, total) {
  return '<div class="cov">' + LEANS.map((k) => {
    const pct = total ? (leans[k] / total) * 100 : 0;
    return pct > 0
      ? `<span class="s-${k}" style="width:${pct.toFixed(2)}%" title="${LABEL[k]}: ${leans[k]}"></span>`
      : '';
  }).join('') + '</div>';
}

function legend(leans) {
  return '<div class="legend">' + LEANS.filter((k) => leans[k] > 0).map((k) =>
    `<span><i class="s-${k}" style="background:var(--${
      { 'left': 'L', 'lean-left': 'LL', 'center': 'C', 'lean-right': 'LR', 'right': 'R' }[k]
    })"></i>${LABEL[k]} ${leans[k]}</span>`).join('') + '</div>';
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function storyHTML(s) {
  const groups = LEANS.map((k) => {
    const arts = s.articles.filter((a) => a.lean === k);
    if (!arts.length) return '';
    const v = { 'left': 'L', 'lean-left': 'LL', 'center': 'C', 'lean-right': 'LR', 'right': 'R' }[k];
    return `<div class="grp"><h4><i style="background:var(--${v})"></i>${LABEL[k]}</h4>` +
      arts.map((a) =>
        `<div class="art"><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>` +
        `<div class="who">${esc(a.outlet)}</div></div>`).join('') + '</div>';
  }).join('');

  const flag = s.blindspot
    ? `<span class="flag">${s.blindspot === 'right' ? 'Right' : 'Left'} blindspot</span>` : '';

  return `<article class="story" data-id="${s.id}">
    <div class="story-head">
      <h3>${esc(s.title)}</h3>
      ${covBar(s.leans, s.total)}
      <div class="counts">
        <span><b>${s.outlets}</b> outlets · ${s.total} articles</span>${flag}
      </div>
      ${legend(s.leans)}
    </div>
    <div class="arts">${groups}</div>
  </article>`;
}

function render() {
  const list = visible();
  const main = $('#main');
  if (!list.length) {
    main.innerHTML = '<p class="loading">No stories match.</p>';
    return;
  }
  main.innerHTML = list.map(storyHTML).join('');
  main.querySelectorAll('.story-head').forEach((h) => {
    h.onclick = () => h.parentElement.classList.toggle('open');
  });
}

boot();
