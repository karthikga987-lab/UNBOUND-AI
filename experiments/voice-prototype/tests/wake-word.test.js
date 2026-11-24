const assert = require("node:assert/strict");
const test = require("node:test");
const { WakeWordController } = require("../wake-word-controller.js");

function fakeDetector() {
  let onDetection;
  let onError;
  const detector = {
    localOnly: true,
    stopped: 0,
    async start(detect, error) { onDetection = detect; onError = error; },
    async stop() { detector.stopped += 1; },
    detect(event) { onDetection?.(event); },
    fail(error) { onError?.(error); },
  };
  return detector;
}

test("only a confident exact wake phrase activates once and stops the detector", async () => {
  let activations = 0;
  const detector = fakeDetector();
  const wake = new WakeWordController({ onActivate: () => { activations += 1; } });
  await wake.start(detector);
  detector.detect({ phrase: "hey unbound", confidence: 0.3 });
  detector.detect({ phrase: "hey unboundish", confidence: 0.99 });
  assert.equal(activations, 0);
  detector.detect({ phrase: " Hey   Unbound ", confidence: 0.9 });
  detector.detect({ phrase: "hey unbound", confidence: 0.99 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(activations, 1);
  assert.equal(detector.stopped, 1);
  assert.equal(wake.state, "activated");
});

test("permission denial and interruption stop without rearming", async () => {
  const detector = fakeDetector();
  const wake = new WakeWordController();
  await wake.start(detector);
  detector.fail({ name: "NotAllowedError" });
  await Promise.resolve();
  assert.equal(wake.state, "permission_denied");
  assert.equal(detector.stopped, 1);
});

test("mute and disable stop capture; stale detections cannot reactivate", async () => {
  let activations = 0;
  const detector = fakeDetector();
  const wake = new WakeWordController({ onActivate: () => { activations += 1; } });
  await wake.start(detector);
  await wake.mute();
  detector.detect({ phrase: "hey unbound", confidence: 1 });
  assert.equal(wake.state, "muted");
  assert.equal(activations, 0);
  await wake.disable();
  assert.equal(await wake.start(detector), false);
  assert.equal(activations, 0);
});

test("browser or cloud recognition adapters are rejected as wake detectors", async () => {
  const wake = new WakeWordController();
  await assert.rejects(wake.start({ localOnly: false, start() {}, stop() {} }), /local wake-word detector/);
  assert.equal(wake.state, "unavailable");
});

test("restarting waits for the previous detector to release the microphone", async () => {
  let releaseStop;
  let secondStarts = 0;
  const first = fakeDetector();
  first.stop = () => new Promise((resolve) => { releaseStop = () => { first.stopped += 1; resolve(); }; });
  const second = fakeDetector();
  second.start = async () => { secondStarts += 1; };
  const wake = new WakeWordController();
  await wake.start(first);
  const stopping = wake.mute();
  await Promise.resolve();
  const restarting = wake.start(second);
  await Promise.resolve();
  assert.equal(secondStarts, 0);
  releaseStop();
  await stopping;
  assert.equal(await restarting, true);
  assert.equal(secondStarts, 1);
  assert.equal(wake.state, "listening");
});
