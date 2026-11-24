/*
 * Experimental adapter boundary for a genuinely local wake-word engine.
 * This module does not capture audio or perform detection by itself.
 */
(function attachWakeWordController(root) {
  class WakeWordController {
    constructor({ onActivate, onState = () => {}, onError = () => {}, threshold = 0.82 } = {}) {
      this.onActivate = onActivate || (() => {});
      this.onState = onState;
      this.onError = onError;
      this.threshold = threshold;
      this.detector = null;
      this.state = "disabled";
      this.enabled = true;
      this.generation = 0;
      this.stopPromise = null;
    }

    async start(detector) {
      if (!this.enabled) return false;
      if (this.stopPromise) await this.stopPromise;
      if (!this.enabled) return false;
      if (this.state === "listening") return false;
      if (!detector || detector.localOnly !== true || typeof detector.start !== "function" || typeof detector.stop !== "function") {
        this._setState("unavailable");
        throw new Error("A verified local wake-word detector is not installed.");
      }
      const run = ++this.generation;
      this.detector = detector;
      try {
        await detector.start(
          (event) => this._onDetection(event, run),
          (error) => this._onError(error, run),
        );
        if (run === this.generation && this.enabled) this._setState("listening");
        return run === this.generation && this.enabled;
      } catch (error) {
        if (run === this.generation) {
          this.detector = null;
          this._setState("error");
        }
        throw error;
      }
    }

    async mute() {
      await this._stop("muted");
    }

    async disable() {
      this.enabled = false;
      await this._stop("disabled");
    }

    async enable() {
      this.enabled = true;
      this._setState("ready");
    }

    async _stop(state) {
      const run = ++this.generation;
      const detector = this.detector;
      this.detector = null;
      if (detector) {
        this.stopPromise = Promise.resolve().then(() => detector.stop()).catch(() => {});
      }
      const pendingStop = this.stopPromise;
      if (pendingStop) await pendingStop;
      if (this.stopPromise === pendingStop) this.stopPromise = null;
      if (run === this.generation) this._setState(this.enabled ? state : "disabled");
    }

    _onDetection(event, run) {
      if (run !== this.generation || !this.enabled || this.state !== "listening") return;
      const phrase = String(event?.phrase || "").trim().toLowerCase().replace(/\s+/g, " ");
      const confidence = Number(event?.confidence);
      if (phrase !== "hey unbound" || !Number.isFinite(confidence) || confidence < this.threshold) return;
      ++this.generation;
      const detector = this.detector;
      this.detector = null;
      this._setState("activated");
      Promise.resolve(detector?.stop()).catch(() => {}).then(() => {
        if (this.state === "activated" && this.enabled) this.onActivate();
      });
    }

    _onError(error, run) {
      if (run !== this.generation) return;
      ++this.generation;
      const detector = this.detector;
      this.detector = null;
      Promise.resolve(detector?.stop()).catch(() => {});
      this._setState(error?.name === "NotAllowedError" ? "permission_denied" : "interrupted");
      this.onError(error);
    }

    _setState(state) {
      this.state = state;
      this.onState(state);
    }
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { WakeWordController };
  if (root) root.UNBOUNDWakeWordController = WakeWordController;
})(typeof globalThis !== "undefined" ? globalThis : this);
