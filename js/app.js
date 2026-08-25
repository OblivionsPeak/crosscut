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

  const bits = [];
  if (DATA.failures && DATA.failures.length) {
    bits.push(['bad', '<b>' + DATA.failures.length + ' feed(s) failed this run:</b> ' +
      DATA.failures.map((f) => esc(f.outlet)).join(', ') +
      ' — those outlets are missing from the coverage above.']);
  }
  if (DATA.healing && DATA.healing.length) {
    bits.push(['', '<b>Self-healing:</b> ' + DATA.healing.map((h) =>
      esc(h.outlet) + ' → ' + esc(h.action)).join('; ') + '.']);
  }
  if (DATA.baseline) {
    bits.push(['', 'Baseline participation on an average story: <b>' + DATA.baseline.left +
      '</b> left-of-centre outlets, <b>' + DATA.baseline.right +
      '</b> right-of-centre. Blindspots are measured against these, not raw counts.']);
  }
  bits.push(['', learnedAxisNote()]);
  $('#health').innerHTML = bits.filter((b) => b[1])
    .map((b) => '<p class="' + b[0] + '">' + b[1] + '</p>').join('');
}

function learnedAxisNote() {
  const a = DATA.axis;
  if (!a) {
    return 'The learned co-coverage axis needs more history before it will report ' +
           'anything (run ' + (DATA.run || 1) + ' so far).';
  }
  const r = a.agreement_with_hand_labels;
  const base = 'Learned co-coverage axis: <b>' + a.stories_observed +
    '</b> stories observed across <b>' + a.runs + '</b> runs, correlation with the ' +
    'hand-curated leans <b>r = ' + r + '</b>. ';
  return base + (a.confident
    ? 'This is now considered stable enough to be meaningful.'
    : 'Still below the confidence bar, so it is reported but not used for anything. ' +
      'It measures which outlets pick the same stories, which is related to political ' +
      'lean but is not the same thing.');
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

function ageLabel(s) {
  if (s.age_hours == null) return '';
  if (s.age_hours < 1.5) return 'new';
  if (s.age_hours < 24) return Math.round(s.age_hours) + 'h old';
  return Math.round(s.age_hours / 24) + 'd old';
}

function pickupLabel(s) {
  if (!s.pickup) return '';
  const side = s.pickup.first === 'left' ? 'Left' : 'Right';
  const other = s.pickup.first === 'left' ? 'right' : 'left';
  const h = s.pickup.hours;
  const t = h < 24 ? h + 'h' : Math.round(h / 24) + 'd';
  return `<span class="pickup">${side} covered this ${t} before the ${other}</span>`;
}

function blindspotTitle(s) {
  const d = s.blindspot_detail;
  if (!d) return 'One side is largely not covering this story';
  const side = s.blindspot === 'right' ? d.right : d.left;
  return `${side.observed} outlet(s) covering, against a baseline of about ` +
         `${side.expected} for that side on an average story`;
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
    ? `<span class="flag" title="${esc(blindspotTitle(s))}">${
        s.blindspot === 'right' ? 'Right' : 'Left'} blindspot</span>` : '';
  const age = ageLabel(s);

  return `<article class="story" data-id="${esc(s.id)}">
    <div class="story-head">
      <h3>${esc(s.title)}</h3>
      ${covBar(s.leans, s.total)}
      <div class="counts">
        <span><b>${s.outlets}</b> outlets · ${s.total} articles</span>
        ${age ? `<span class="age">${age}</span>` : ''}
        ${flag}${pickupLabel(s)}
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
