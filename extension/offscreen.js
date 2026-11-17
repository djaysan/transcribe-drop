// Records the tab picture background.js hands over and streams it to the app as it goes:
// the recorder hands data over at each keyframe, one every 10 s, so a crash loses at most
// ~10 s. H.264 MP4 from the browser's own encoder, at most 1920×1080 and 5 frames a second:
// plenty for slides and shared screens, and a still slide costs almost nothing.
// Stops when background.js says so, when the tab closes, or when the app's meeting ends.
const APP = 'http://127.0.0.1:47913';
let rec = null;

chrome.runtime.onMessage.addListener(m => {
  if (m.stop && rec?.state === 'recording') rec.stop();
  if (m.streamId) record(m.streamId).catch(e => done(e.message));
});

function done(error) {
  chrome.runtime.sendMessage({ done: true, error });
}

async function record(streamId) {
  // Video only: the app already records both sides of the call, and capturing the tab's
  // sound would mute it for you.
  const stream = await navigator.mediaDevices.getUserMedia({ video: { mandatory: {
    chromeMediaSource: 'tab', chromeMediaSourceId: streamId, maxWidth: 1920, maxHeight: 1080, maxFrameRate: 5,
    minFrameRate: 1,   // a still tab only sends frames when asked: this resends it every second
  } } });
  const start = Date.now();   // names this recording in every request
  const post = (path, body, more = '') => fetch(`${APP}${path}?start=${start}${more}`, { method: 'POST', body });
  let sent = Promise.resolve();   // uploads go one after the other, in order
  rec = new MediaRecorder(stream, {
    mimeType: 'video/mp4;codecs=avc1', videoBitsPerSecond: 800_000, videoKeyFrameIntervalDuration: 10_000,
  });
  rec.ondataavailable = e => { sent = sent.then(() => post('/video', e.data)).catch(() => {}); };
  rec.onstop = async () => {
    stream.getTracks().forEach(t => t.stop());
    await sent;
    await post('/video/done', null, `&first=${first || start}`).catch(() => {});
    done();
  };
  stream.getVideoTracks()[0].onended = () => rec.state === 'recording' && rec.stop();   // tab closed
  rec.start(5000);
  // The video's 0:00 is its first frame, not when recording started: the app lines the
  // video up with the audio by this.
  let first = 0;
  new ImageCapture(stream.getVideoTracks()[0]).grabFrame().then(() => { first = Date.now(); }, () => {});
  // The meeting stopped, or the app quit → stop too.
  const poll = setInterval(async () => {
    const on = await post('/state').then(r => r.json()).then(s => s.recording).catch(() => false);
    if (!on && rec.state === 'recording') rec.stop();
    if (rec.state !== 'recording') clearInterval(poll);
  }, 3000);
}
