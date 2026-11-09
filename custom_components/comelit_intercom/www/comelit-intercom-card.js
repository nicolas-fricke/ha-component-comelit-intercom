/**
 * Privacy-aware Comelit camera card.
 *
 * Positive intervals request one still through Home Assistant's camera proxy.
 * Zero embeds the normal live camera card. Work pauses whenever this element
 * or its browser tab is not visible.
 */
class ComelitIntercomCard extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._config = null;
    this._connected = false;
    this._visible = false;
    this._timer = null;
    this._captureController = null;
    this._lastStillAt = null;
    this._objectUrl = null;
    this._liveCard = null;
    this._liveCardPromise = null;
    this._policyGeneration = 0;
    this._observer = null;
    this._visibilityHandler = null;
    this.attachShadow({ mode: "open" });
  }

  setConfig(config) {
    const cameraEntity = config.camera_entity || config.entity;
    if (!cameraEntity || !cameraEntity.startsWith("camera.")) {
      throw new Error("camera_entity must be a Home Assistant camera entity");
    }
    if (this._config) this._pause();
    this._config = {
      ...config,
      camera_entity: cameraEntity,
      preview_switch:
        config.preview_switch || "switch.comelit_intercom_automatic_still_previews",
      preview_interval:
        config.preview_interval || "number.comelit_intercom_still_preview_interval",
    };
    this._render();
    this._applyPolicy();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._liveCard) this._liveCard.hass = hass;
    this._applyPolicy();
  }

  connectedCallback() {
    this._connected = true;
    this._observer = new IntersectionObserver((entries) => {
      this._visible = Boolean(entries[0]?.isIntersecting);
      this._applyPolicy();
    });
    this._observer.observe(this);
    this._visibilityHandler = () => this._applyPolicy();
    document.addEventListener("visibilitychange", this._visibilityHandler);
    this._applyPolicy();
  }

  disconnectedCallback() {
    this._connected = false;
    this._visible = false;
    this._observer?.disconnect();
    this._observer = null;
    document.removeEventListener("visibilitychange", this._visibilityHandler);
    this._visibilityHandler = null;
    this._pause();
    this._revokeObjectUrl();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return {
      type: "custom:comelit-intercom-card",
      camera_entity: "camera.comelit_intercom_live_feed",
      preview_switch: "switch.comelit_intercom_automatic_still_previews",
      preview_interval: "number.comelit_intercom_still_preview_interval",
    };
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { overflow: hidden; }
        .frame {
          position: relative;
          width: 100%;
          aspect-ratio: 5 / 3;
          overflow: hidden;
          background: #111;
          cursor: pointer;
        }
        .still, .live-slot {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
        }
        .still {
          display: flex;
          align-items: center;
          justify-content: center;
          color: rgba(255, 255, 255, 0.82);
        }
        .still img {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          object-fit: cover;
        }
        .camera-icon {
          width: 56px;
          height: 56px;
          fill: currentColor;
          opacity: 0.72;
        }
        .badge {
          position: absolute;
          z-index: 3;
          left: 9px;
          top: 9px;
          padding: 4px 8px;
          border-radius: 4px;
          color: white;
          background: rgba(0, 0, 0, 0.66);
          font-size: 12px;
          line-height: 1.25;
          pointer-events: none;
        }
        .live-slot { display: none; }
        .live-slot > * { width: 100%; height: 100%; }
        .live-dot::before {
          content: "";
          display: inline-block;
          width: 7px;
          height: 7px;
          margin-right: 6px;
          border-radius: 50%;
          background: #f44336;
        }
      </style>
      <ha-card>
        <div class="frame" id="frame" title="Open live video">
          <div class="still" id="still">
            <svg class="camera-icon" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2m0 2v12h16V6H4m4 2a2 2 0 1 1 0 4 2 2 0 0 1 0-4m-2 8 3-3 2 2 3-3 4 4H6Z"/>
            </svg>
            <img id="image" alt="Comelit intercom preview" hidden />
          </div>
          <div class="live-slot" id="live-slot"></div>
          <div class="badge" id="badge">Automatic previews off</div>
        </div>
      </ha-card>
    `;
    this.shadowRoot.getElementById("frame").addEventListener("click", () => {
      this.dispatchEvent(new CustomEvent("hass-more-info", {
        bubbles: true,
        composed: true,
        detail: { entityId: this._config.camera_entity },
      }));
    });
  }

  _settings() {
    const switchState = this._hass?.states[this._config?.preview_switch];
    const numberState = this._hass?.states[this._config?.preview_interval];
    const interval = Number.parseFloat(numberState?.state);
    return {
      enabled: switchState?.state === "on",
      interval: Number.isFinite(interval) ? interval : null,
    };
  }

  _isVisible() {
    return this._connected && this._visible && document.visibilityState === "visible";
  }

  _applyPolicy() {
    if (!this._hass || !this._config || !this.shadowRoot.firstElementChild) return;
    const { enabled, interval } = this._settings();

    if (!this._isVisible()) {
      this._pause();
      return;
    }
    if (!enabled) {
      this._pause();
      this._showStill();
      if (this._lastStillAt) this._updateStillBadge();
      else this._setBadge("Automatic previews off");
      return;
    }
    if (interval === null) {
      this._pause();
      this._showStill();
      if (this._lastStillAt) this._updateStillBadge();
      else this._setBadge("Preview interval unavailable");
      return;
    }
    if (interval === 0) {
      this._clearTimer();
      this._abortCapture();
      this._showLive();
      return;
    }

    this._removeLiveCard();
    this._showStill();
    if (!this._timer && !this._captureController) {
      const elapsed = this._lastStillAt ? Date.now() - this._lastStillAt.getTime() : Infinity;
      const delay = Math.max(0, interval * 60_000 - elapsed);
      this._scheduleCapture(delay);
    }
    this._updateStillBadge();
  }

  _scheduleCapture(delay) {
    this._clearTimer();
    this._timer = window.setTimeout(() => {
      this._timer = null;
      this._captureStill();
    }, Math.max(100, delay));
  }

  async _captureStill() {
    const { enabled, interval } = this._settings();
    if (!this._isVisible() || !enabled || interval === null || interval <= 0) return;
    const cameraState = this._hass.states[this._config.camera_entity];
    const token = cameraState?.attributes?.access_token;
    if (!token) {
      this._setBadge("Camera unavailable");
      this._scheduleCapture(interval * 60_000);
      return;
    }

    const generation = this._policyGeneration;
    const controller = new AbortController();
    this._captureController = controller;
    if (!this._lastStillAt) this._setBadge("Taking preview…");
    try {
      const entity = encodeURIComponent(this._config.camera_entity);
      const url = `/api/camera_proxy/${entity}?token=${encodeURIComponent(token)}&t=${Date.now()}`;
      const response = await fetch(url, {
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const current = this._settings();
      if (
        generation !== this._policyGeneration ||
        !this._isVisible() ||
        !current.enabled ||
        current.interval === null ||
        current.interval <= 0
      ) return;

      this._revokeObjectUrl();
      this._objectUrl = URL.createObjectURL(blob);
      const image = this.shadowRoot.getElementById("image");
      image.src = this._objectUrl;
      image.hidden = false;
      this._lastStillAt = new Date();
      this._updateStillBadge();
    } catch (error) {
      if (error.name !== "AbortError") {
        console.warn("Comelit still preview failed", error);
        if (this._lastStillAt) this._updateStillBadge();
        else this._setBadge("Preview failed");
      }
    } finally {
      if (this._captureController === controller) {
        this._captureController = null;
      }
      const current = this._settings();
      if (
        generation === this._policyGeneration &&
        this._isVisible() &&
        current.enabled &&
        current.interval > 0
      ) {
        this._scheduleCapture(current.interval * 60_000);
      }
    }
  }

  async _showLive() {
    if (this._liveCard || this._liveCardPromise) return;
    const generation = this._policyGeneration;
    this._showStill();
    this._setBadge("Starting live…");
    const liveCardPromise = (async () => {
      const helpers = await window.loadCardHelpers();
      const settings = this._settings();
      if (
        generation !== this._policyGeneration ||
        !this._isVisible() ||
        !settings.enabled ||
        settings.interval !== 0
      ) return;
      const card = await helpers.createCardElement({
        type: "picture-entity",
        entity: this._config.camera_entity,
        camera_view: "live",
        show_name: false,
        show_state: false,
        tap_action: { action: "none" },
        hold_action: { action: "none" },
      });
      const current = this._settings();
      if (
        generation !== this._policyGeneration ||
        !this._isVisible() ||
        !current.enabled ||
        current.interval !== 0
      ) return;
      card.hass = this._hass;
      this._liveCard = card;
      const slot = this.shadowRoot.getElementById("live-slot");
      slot.replaceChildren(card);
      slot.style.display = "block";
      this.shadowRoot.getElementById("still").style.display = "none";
      this._setBadge("Live", true);
    })();
    this._liveCardPromise = liveCardPromise;
    try {
      await liveCardPromise;
    } catch (error) {
      console.warn("Comelit live preview failed", error);
      this._setBadge("Live preview failed");
    } finally {
      if (this._liveCardPromise === liveCardPromise) {
        this._liveCardPromise = null;
      }
    }
  }

  _showStill() {
    const still = this.shadowRoot.getElementById("still");
    if (still) still.style.display = "flex";
    const slot = this.shadowRoot.getElementById("live-slot");
    if (slot) slot.style.display = "none";
  }

  _removeLiveCard() {
    this._policyGeneration += 1;
    this._liveCard?.remove();
    this._liveCard = null;
    const slot = this.shadowRoot.getElementById("live-slot");
    if (slot) slot.replaceChildren();
  }

  _pause() {
    this._clearTimer();
    this._abortCapture();
    this._removeLiveCard();
    this._showStill();
  }

  _clearTimer() {
    if (this._timer !== null) {
      window.clearTimeout(this._timer);
      this._timer = null;
    }
  }

  _abortCapture() {
    this._captureController?.abort();
    this._captureController = null;
  }

  _updateStillBadge() {
    if (!this._lastStillAt) {
      this._setBadge("Waiting for preview…");
      return;
    }
    const time = this._lastStillAt.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    this._setBadge(time);
    this.shadowRoot.getElementById("badge").title = this._lastStillAt.toLocaleString();
  }

  _setBadge(text, live = false) {
    const badge = this.shadowRoot.getElementById("badge");
    if (!badge) return;
    badge.textContent = text;
    badge.classList.toggle("live-dot", live);
  }

  _revokeObjectUrl() {
    if (!this._objectUrl) return;
    URL.revokeObjectURL(this._objectUrl);
    this._objectUrl = null;
  }
}

if (!customElements.get("comelit-intercom-card")) {
  customElements.define("comelit-intercom-card", ComelitIntercomCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "comelit-intercom-card",
  name: "Comelit Intercom Camera",
  description: "Privacy-aware timestamped previews with click-to-live video.",
});
