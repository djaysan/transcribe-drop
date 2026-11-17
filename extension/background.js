// Transcribe Drop for Brave/Chrome: the toolbar icon (or ⌥⇧R) records the current tab's
// picture into the app's meeting recording; starts that recording too if it isn't running.
// Clicking again stops the video only; the app keeps recording the audio until the call ends.
// Tab recording can only start from a click or the shortcut: browsers don't allow it otherwise.
// The recording itself runs in offscreen.html (a service worker can't record).
// manifest.json "key" pins this extension's ID, which the app accepts requests from.
const APP = 'http://127.0.0.1:47913';   // transcribe-app.py EXT_PORT, this Mac only

function badge(text, title, color = '#dc2626') {
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setBadgeText({ text });
  chrome.action.setTitle({ title });
}

chrome.action.onClicked.addListener(async tab => {
  if (await chrome.offscreen.hasDocument()) {   // already recording → stop the video
    chrome.runtime.sendMessage({ stop: true });
    return;
  }
  try {
    const r = await fetch(`${APP}/start`, { method: 'POST' });
    if (!r.ok) throw new Error(await r.text());
  } catch (e) {
    // fetch itself fails when nothing listens: the app isn't open
    const why = e instanceof TypeError ? 'open the Transcribe Drop app first' : e.message;
    return badge('!', `Transcribe Drop: ${why}`);
  }
  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  await chrome.offscreen.createDocument({
    url: 'offscreen.html', reasons: ['USER_MEDIA'], justification: "Records the tab's video for Transcribe Drop",
  });
  chrome.runtime.sendMessage({ streamId });
  badge('REC', 'Transcribe Drop: recording this tab (click to stop the video)');
});

// While the app records a meeting without this video (auto-record started it), the icon asks
// for the click: an amber VID badge. Checked every 30 s, the shortest alarm Chrome allows.
chrome.alarms.get('nag').then(a => a || chrome.alarms.create('nag', { periodInMinutes: 0.5 }));
chrome.alarms.onAlarm.addListener(nag);

async function nag() {
  if (await chrome.offscreen.hasDocument()) return;   // the video is recording
  const on = await fetch(`${APP}/state`, { method: 'POST' })
    .then(r => r.json()).then(s => s.recording).catch(() => false);
  if (on) {
    badge('VID', 'Transcribe Drop is recording this meeting without video: click here (or ⌥⇧R) on the call tab to add it', '#f59e0b');
  } else if (await chrome.action.getBadgeText({}) === 'VID') {   // leaves an error badge alone
    badge('', "Transcribe Drop: record this tab's video");
  }
}

// From offscreen.js: the video stopped (click, tab closed, or the meeting ended), or failed.
chrome.runtime.onMessage.addListener(m => {
  if (!m.done) return;
  chrome.offscreen.closeDocument();
  if (m.error) badge('!', `Transcribe Drop: ${m.error}`);
  else badge('', "Transcribe Drop: record this tab's video");
});
