/* Shared per-video progress for SSFlix and playlist downloads. */
window.DownloadQueue = class DownloadQueue {
  constructor(container, videos) {
    this.rows = videos.map((video, index) => {
      const row = document.createElement('li');
      row.className = 'rounded-xl border border-white/10 bg-black/20 p-3 min-w-0';
      row.innerHTML = `<div class="flex flex-wrap items-start justify-between gap-2">
        <p class="queue-title text-sm text-white break-words min-w-0 flex-1"></p>
        <span class="queue-state text-xs text-slate-400"></span>
      </div>
      <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-white/10" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <div class="queue-bar h-full bg-violet-400 transition-all" style="width:0%"></div>
      </div>
      <p class="queue-details mt-2 text-xs text-slate-400 break-words"></p>`;
      row.querySelector('.queue-title').textContent = `${index + 1}. ${video.title || video.url}`;
      const progress = row.querySelector('[role=progressbar]');
      progress.setAttribute('aria-label', video.title || `Video ${index + 1}`);
      container.appendChild(row);
      return {video, row, progress};
    });
    this.update({items: videos.map(v => ({url: v.url, status: 'queued', percent: 0}))});
  }
  update(job) {
    const items = new Map((job.items || []).map(item => [item.url, item]));
    const bytes = n => n >= 1e9 ? `${(n/1e9).toFixed(2)} GB` : `${(n/1e6).toFixed(1)} MB`;
    this.rows.forEach(({video, row, progress}) => {
      const item = items.get(video.url);
      if (!item) return;
      if (!video.title && item.title) row.querySelector('.queue-title').textContent = `${this.rows.findIndex(entry => entry.row === row) + 1}. ${item.title}`;
      const percent = Math.max(0, Math.min(100, Number(item.percent) || 0));
      const labels = {queued:'Waiting', starting:'Finding video / audio…', downloading:`Downloading · ${percent}%`, processing:'Merging / saving…', done:'Finished · 100%', error:'Failed'};
      row.dataset.status = item.status;
      row.querySelector('.queue-state').textContent = labels[item.status] || item.status;
      row.querySelector('.queue-state').className = 'queue-state text-xs ' + (item.status === 'done' ? 'text-emerald-300' : item.status === 'error' ? 'text-red-300' : 'text-slate-300');
      row.querySelector('.queue-bar').style.width = percent + '%';
      row.querySelector('.queue-bar').style.backgroundColor = item.status === 'error' ? '#f87171' : item.status === 'done' ? '#34d399' : '#a78bfa';
      progress.setAttribute('aria-valuenow', String(percent));
      const details = [];
      if (item.status === 'error') details.push(item.error || 'Download failed.');
      else if (item.status === 'done') details.push(item.filename || 'Saved successfully');
      else if (item.status === 'queued') details.push('Downloads start one by one.');
      else {
        if (item.downloaded_bytes) details.push(bytes(item.downloaded_bytes) + (item.total_bytes ? ` / ~${bytes(item.total_bytes)}` : ' downloaded'));
        if (item.speed) details.push(`${bytes(item.speed)}/s`);
        if (item.eta != null) details.push(`${Math.floor(item.eta/60)}m ${Math.round(item.eta%60)}s remaining`);
      }
      row.querySelector('.queue-details').textContent = details.join(' · ');
    });
  }
};

// A job link keeps the progress visible after refreshing or reopening the page.
window.trackDownloadJob = async function(container, jobId) {
  const summary = document.createElement('p');
  summary.className = 'text-sm text-slate-300';
  const list = document.createElement('ol');
  list.className = 'mt-4 space-y-3';
  list.id = 'trackedVideoProgress';
  container.append(summary, list);
  let queue;
  try {
    while (true) {
      const response = await fetch('/api/progress/' + encodeURIComponent(jobId));
      const job = await response.json();
      if (!response.ok) throw new Error(job.detail || 'Could not load download progress.');
      if (!queue) queue = new DownloadQueue(list, (job.items || []).map(item => ({url:item.url, title:item.title})));
      queue.update(job);
      const server = job.ssflix_server ? ` · Server ${job.ssflix_server}` : '';
      summary.textContent = job.status === 'done'
        ? `${job.done || 0} downloaded, ${job.failed || 0} failed${server} → ${job.folder || ''}`
        : `Video ${job.current || 1} of ${job.total || job.items.length}${server} · ${job.current_title || job.status}`;
      if (job.status === 'error') throw new Error(job.error || 'Download failed.');
      if (job.status === 'done') break;
      await new Promise(resolve => setTimeout(resolve, 900));
    }
  } catch (error) {
    summary.textContent = error.message;
  }
};
