/**
 * Lumen-Zero High-Performance Frontend Engine.
 * Handles client-side heavy lifting, reactive controls, video streaming,
 * FPS calculation, LED strip simulation, and browser LocalStorage persistence.
 */

// Environment Defaults from .env (injected via window.APP_DEFAULTS)
const envDefaults = window.APP_DEFAULTS || {
    anti_flicker: 50,
    anti_flicker_code: 1,
    led_brightness: 45,
    default_led_color: "WHITE",
    led_color_rgb: [255, 255, 255],
    led_color_hex: "#ffffff",
    led_auto_off: 120,
    inactivity_time: 600,
    led_length: 100,
    default_resolution: "1920x1080",
    default_device_cam_fit: "cover"
};

// Global State
const state = {
    // Light state (Initialized with .env defaults)
    lights: {
        power: false,
        color: {
            r: envDefaults.led_color_rgb?.[0] ?? 255,
            g: envDefaults.led_color_rgb?.[1] ?? 255,
            b: envDefaults.led_color_rgb?.[2] ?? 255,
            hex: envDefaults.led_color_hex ?? "#ffffff"
        },
        brightness: (envDefaults.led_brightness ?? 45) / 100.0,
        brightness_pct: envDefaults.led_brightness ?? 45,
        effect: "static",
        speed: 50,
        length: envDefaults.led_length ?? 100,
        auto_off_sec: envDefaults.led_auto_off ?? 120
    },
    // Camera state (Initialized with .env defaults)
    camera: {
        enabled: false,
        active_id: null,
        resolution: envDefaults.default_resolution ?? "1920x1080",
        target_fps: 30,
        digital_zoom: 1.0,
        flip_h: false,
        flip_v: false,
        grid_active: false,
        anti_flicker_code: envDefaults.anti_flicker_code ?? 1,
        filters: {
            brightness: 100,
            contrast: 100,
            saturation: 100
        },
        controls: {}
    },
    // Local Device Camera state (Browser webcam)
    deviceCamera: {
        enabled: false,
        active: false,
        stream: null,
        deviceId: null,
        resolution: "1920x1080",
        digital_zoom: 1.0,
        rotation: 0,
        fit_mode: envDefaults.default_device_cam_fit || "cover",
        flip_h: false,
        flip_v: false,
        grid_active: false,
        focus_mode: "continuous",
        focus_distance: 0,
        filters: {
            brightness: 100,
            contrast: 100,
            saturation: 100,
            sharpness: 100
        }
    },
    system: {
        online: false,
        latency_ms: 0
    }
};

// --- LOCAL STORAGE CACHE HELPERS ---
const STORAGE_KEY = "lumen_zero_user_config_v2";

function loadSavedConfig() {
    try {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (saved) {
            const parsed = JSON.parse(saved);
            if (parsed.lights) {
                // Strip saved power: always boot in safe OFF state until user turns on manually
                delete parsed.lights.power;
                Object.assign(state.lights, parsed.lights);
            }
            if (parsed.camera) {
                // Strip saved enabled: always boot with camera disabled until user turns on manually
                delete parsed.camera.enabled;
                Object.assign(state.camera, parsed.camera);
            }
            if (parsed.deviceCamera) {
                // Strip saved active state: camera only starts when user checks checkbox
                delete parsed.deviceCamera.enabled;
                delete parsed.deviceCamera.active;
                delete parsed.deviceCamera.stream;
                Object.assign(state.deviceCamera, parsed.deviceCamera);
            }
            console.log("[Storage] User configuration loaded from browser storage:", parsed);
        } else {
            console.log("[Config] Initialized with .env defaults:", envDefaults);
        }
    } catch (e) {
        console.warn("[Storage] Could not read localStorage:", e);
    }
}

function saveConfig() {
    try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({
            lights: state.lights,
            camera: {
                active_id: state.camera.active_id,
                resolution: state.camera.resolution,
                target_fps: state.camera.target_fps,
                digital_zoom: state.camera.digital_zoom,
                flip_h: state.camera.flip_h,
                flip_v: state.camera.flip_v,
                grid_active: state.camera.grid_active,
                filters: state.camera.filters
            },
            deviceCamera: {
                deviceId: state.deviceCamera.deviceId,
                resolution: state.deviceCamera.resolution,
                digital_zoom: state.deviceCamera.digital_zoom,
                rotation: state.deviceCamera.rotation,
                fit_mode: state.deviceCamera.fit_mode,
                focus_mode: state.deviceCamera.focus_mode,
                focus_distance: state.deviceCamera.focus_distance,
                flip_h: state.deviceCamera.flip_h,
                flip_v: state.deviceCamera.flip_v,
                grid_active: state.deviceCamera.grid_active,
                filters: state.deviceCamera.filters
            }
        }));
    } catch (e) {
        console.warn("[Storage] Could not save to localStorage:", e);
    }
}

// --- UTILITIES ---
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

function showToast(message) {
    const container = document.getElementById("toastContainer");
    if (!container) return;
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = "0";
        setTimeout(() => toast.remove(), 300);
    }, 2800);
}

// --- LIGHT CONTROLS ---

function initVirtualLedStrip() {
    const container = document.getElementById("virtualLedStrip");
    if (!container) return;
    container.innerHTML = "";
    const NODE_COUNT = 24; // Clean responsive visual nodes that never overflow
    for (let i = 0; i < NODE_COUNT; i++) {
        const node = document.createElement("div");
        node.className = "virtual-led-node";
        node.id = `ledNode_${i}`;
        container.appendChild(node);
    }
}

let animFrameId = null;
let animStep = 0;

function updateLedVisualizer() {
    const nodes = document.querySelectorAll(".virtual-led-node");
    if (!nodes.length) return;

    if (!state.lights.power) {
        nodes.forEach(n => {
            n.style.backgroundColor = "#111827";
            n.style.boxShadow = "none";
        });
        return;
    }

    const { r, g, b } = state.lights.color;
    const brightFactor = state.lights.brightness;
    const effect = state.lights.effect;

    if (effect === "static") {
        const rgbStr = `rgb(${Math.round(r * brightFactor)}, ${Math.round(g * brightFactor)}, ${Math.round(b * brightFactor)})`;
        const glowStr = `0 0 10px rgba(${r}, ${g}, ${b}, ${brightFactor * 0.9})`;
        nodes.forEach(n => {
            n.style.backgroundColor = rgbStr;
            n.style.boxShadow = glowStr;
        });
    } else if (effect === "rainbow") {
        animStep = (animStep + 2) % 360;
        nodes.forEach((n, i) => {
            const hue = (animStep + (i * 360 / nodes.length)) % 360;
            n.style.backgroundColor = `hsl(${hue}, 100%, ${Math.round(50 * brightFactor)}%)`;
            n.style.boxShadow = `0 0 8px hsl(${hue}, 100%, 50%)`;
        });
    } else if (effect === "breathing") {
        animStep = (animStep + 0.05) % (Math.PI * 2);
        const pulse = (Math.sin(animStep) + 1) / 2 * brightFactor;
        const rgbStr = `rgb(${Math.round(r * pulse)}, ${Math.round(g * pulse)}, ${Math.round(b * pulse)})`;
        nodes.forEach(n => {
            n.style.backgroundColor = rgbStr;
            n.style.boxShadow = `0 0 10px rgba(${r}, ${g}, ${b}, ${pulse})`;
        });
    } else if (effect === "marquee") {
        animStep = (animStep + 1) % 6;
        nodes.forEach((n, i) => {
            if ((i + animStep) % 6 === 0) {
                n.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
                n.style.boxShadow = `0 0 10px rgba(${r}, ${g}, ${b}, ${brightFactor})`;
            } else {
                n.style.backgroundColor = "#111827";
                n.style.boxShadow = "none";
            }
        });
    } else {
        // Fallback solid for other dynamic effects
        const rgbStr = `rgb(${Math.round(r * brightFactor)}, ${Math.round(g * brightFactor)}, ${Math.round(b * brightFactor)})`;
        nodes.forEach(n => {
            n.style.backgroundColor = rgbStr;
            n.style.boxShadow = `0 0 8px rgba(${r}, ${g}, ${b}, ${brightFactor})`;
        });
    }
}

function startVisualizerLoop() {
    function loop() {
        updateLedVisualizer();
        animFrameId = requestAnimationFrame(loop);
    }
    if (animFrameId) cancelAnimationFrame(animFrameId);
    animFrameId = requestAnimationFrame(loop);
}

async function executeLightUpdate(immediate = false) {
    try {
        const payload = {
            power: state.lights.power,
            color: [state.lights.color.r, state.lights.color.g, state.lights.color.b],
            brightness: state.lights.brightness,
            effect: state.lights.effect,
            speed: state.lights.speed
        };
        const res = await fetch("/api/lights", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        if (res.ok) {
            saveConfig();
        }
    } catch (e) {
        console.error("Failed to send light update:", e);
    }
}

const sendLightUpdateDebounced = debounce(() => executeLightUpdate(false), 120);

function sendLightUpdate(immediate = false) {
    if (immediate) {
        executeLightUpdate(true);
    } else {
        sendLightUpdateDebounced();
    }
}

// --- REINVENTED COLOR WHEEL CLASS ---
class ReinventedColorWheel {
    constructor(canvas, options = {}) {
        this.canvas = canvas;
        this.ctx = canvas.getContext("2d");
        this.onChange = options.onChange || (() => { });

        // Color state: h in [0, 360), s in [0, 1], v in [0, 1]
        this.h = 0;
        this.s = 0;
        this.v = 1.0;

        this.width = canvas.width;
        this.height = canvas.height;
        this.cx = this.width / 2;
        this.cy = this.height / 2;

        this.outerRadius = Math.round(this.width * 0.44);
        this.innerRadius = Math.round(this.width * 0.33);
        this.boxHalfSize = Math.round(this.width * 0.20);

        this.isDraggingHue = false;
        this.isDraggingSV = false;

        this._initEvents();
        this.redraw();
    }

    _initEvents() {
        const getPos = (e) => {
            const rect = this.canvas.getBoundingClientRect();
            const clientX = e.touches ? e.touches[0].clientX : e.clientX;
            const clientY = e.touches ? e.touches[0].clientY : e.clientY;
            return {
                x: (clientX - rect.left) * (this.width / rect.width),
                y: (clientY - rect.top) * (this.height / rect.height)
            };
        };

        const onStart = (e) => {
            const pos = getPos(e);
            const dx = pos.x - this.cx;
            const dy = pos.y - this.cy;
            const dist = Math.sqrt(dx * dx + dy * dy);

            if (dist >= this.innerRadius - 6 && dist <= this.outerRadius + 8) {
                e.preventDefault();
                this.isDraggingHue = true;
                this._updateHue(dx, dy);
            } else if (Math.abs(dx) <= this.boxHalfSize + 8 && Math.abs(dy) <= this.boxHalfSize + 8) {
                e.preventDefault();
                this.isDraggingSV = true;
                this._updateSV(dx, dy);
            }
        };

        const onMove = (e) => {
            if (!this.isDraggingHue && !this.isDraggingSV) return;
            e.preventDefault();
            const pos = getPos(e);
            const dx = pos.x - this.cx;
            const dy = pos.y - this.cy;

            if (this.isDraggingHue) {
                this._updateHue(dx, dy);
            } else if (this.isDraggingSV) {
                this._updateSV(dx, dy);
            }
        };

        const onEnd = () => {
            this.isDraggingHue = false;
            this.isDraggingSV = false;
        };

        this.canvas.addEventListener("mousedown", onStart);
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onEnd);

        this.canvas.addEventListener("touchstart", onStart, { passive: false });
        window.addEventListener("touchmove", onMove, { passive: false });
        window.addEventListener("touchend", onEnd);
    }

    _updateHue(dx, dy) {
        let angle = Math.atan2(dy, dx) * (180 / Math.PI);
        if (angle < 0) angle += 360;
        this.h = Math.round(angle) % 360;
        this.redraw();
        this._notify();
    }

    _updateSV(dx, dy) {
        const clampedX = Math.max(-this.boxHalfSize, Math.min(this.boxHalfSize, dx));
        const clampedY = Math.max(-this.boxHalfSize, Math.min(this.boxHalfSize, dy));
        this.s = (clampedX + this.boxHalfSize) / (2 * this.boxHalfSize);
        this.v = 1.0 - (clampedY + this.boxHalfSize) / (2 * this.boxHalfSize);
        this.redraw();
        this._notify();
    }

    _notify() {
        const { r, g, b } = ReinventedColorWheel.hsvToRgb(this.h, this.s, this.v);
        const hex = "#" + [r, g, b].map(x => x.toString(16).padStart(2, "0")).join("");
        this.onChange({ r, g, b, hex, h: this.h, s: this.s, v: this.v });
    }

    setRgb(r, g, b, triggerChange = true) {
        const { h, s, v } = ReinventedColorWheel.rgbToHsv(r, g, b);
        this.h = h;
        this.s = s;
        this.v = v;
        this.redraw();
        if (triggerChange) {
            this._notify();
        }
    }

    redraw() {
        const ctx = this.ctx;
        ctx.clearRect(0, 0, this.width, this.height);

        // 1. Draw Continuous Hue Ring
        const step = 1;
        for (let deg = 0; deg < 360; deg += step) {
            const radStart = (deg - 0.5) * (Math.PI / 180);
            const radEnd = (deg + step + 0.5) * (Math.PI / 180);
            ctx.beginPath();
            ctx.arc(this.cx, this.cy, (this.outerRadius + this.innerRadius) / 2, radStart, radEnd);
            ctx.lineWidth = this.outerRadius - this.innerRadius;
            ctx.strokeStyle = `hsl(${deg}, 100%, 50%)`;
            ctx.stroke();
        }

        // Inner and Outer Subtle Borders
        ctx.beginPath();
        ctx.arc(this.cx, this.cy, this.outerRadius, 0, Math.PI * 2);
        ctx.lineWidth = 1;
        ctx.strokeStyle = "rgba(255, 255, 255, 0.25)";
        ctx.stroke();

        ctx.beginPath();
        ctx.arc(this.cx, this.cy, this.innerRadius, 0, Math.PI * 2);
        ctx.lineWidth = 1;
        ctx.strokeStyle = "rgba(255, 255, 255, 0.25)";
        ctx.stroke();

        // 2. Draw Hue Puck on Ring
        const hueAngle = this.h * (Math.PI / 180);
        const ringMid = (this.outerRadius + this.innerRadius) / 2;
        const puckX = this.cx + ringMid * Math.cos(hueAngle);
        const puckY = this.cy + ringMid * Math.sin(hueAngle);

        ctx.beginPath();
        ctx.arc(puckX, puckY, (this.outerRadius - this.innerRadius) / 2 + 1, 0, Math.PI * 2);
        ctx.fillStyle = `hsl(${this.h}, 100%, 50%)`;
        ctx.fill();
        ctx.lineWidth = 2.5;
        ctx.strokeStyle = "#ffffff";
        ctx.shadowColor = "rgba(0, 0, 0, 0.6)";
        ctx.shadowBlur = 4;
        ctx.stroke();
        ctx.shadowBlur = 0;

        // 3. Draw Center SV Surface (Square)
        const bx = this.cx - this.boxHalfSize;
        const by = this.cy - this.boxHalfSize;
        const bSize = this.boxHalfSize * 2;

        ctx.fillStyle = `hsl(${this.h}, 100%, 50%)`;
        ctx.fillRect(bx, by, bSize, bSize);

        const whiteGrad = ctx.createLinearGradient(bx, by, bx + bSize, by);
        whiteGrad.addColorStop(0, "rgba(255, 255, 255, 1)");
        whiteGrad.addColorStop(1, "rgba(255, 255, 255, 0)");
        ctx.fillStyle = whiteGrad;
        ctx.fillRect(bx, by, bSize, bSize);

        const blackGrad = ctx.createLinearGradient(bx, by, bx, by + bSize);
        blackGrad.addColorStop(0, "rgba(0, 0, 0, 0)");
        blackGrad.addColorStop(1, "rgba(0, 0, 0, 1)");
        ctx.fillStyle = blackGrad;
        ctx.fillRect(bx, by, bSize, bSize);

        ctx.strokeStyle = "rgba(255, 255, 255, 0.3)";
        ctx.lineWidth = 1;
        ctx.strokeRect(bx, by, bSize, bSize);

        // 4. Draw SV Puck
        const svPuckX = bx + this.s * bSize;
        const svPuckY = by + (1.0 - this.v) * bSize;
        const { r, g, b } = ReinventedColorWheel.hsvToRgb(this.h, this.s, this.v);

        ctx.beginPath();
        ctx.arc(svPuckX, svPuckY, 6, 0, Math.PI * 2);
        ctx.fillStyle = `rgb(${r}, ${g}, ${b})`;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = this.v > 0.5 ? "#000000" : "#ffffff";
        ctx.shadowColor = "rgba(0,0,0,0.5)";
        ctx.shadowBlur = 3;
        ctx.stroke();
        ctx.shadowBlur = 0;
    }

    static hsvToRgb(h, s, v) {
        let r, g, b;
        const i = Math.floor((h / 60) % 6);
        const f = (h / 60) - i;
        const p = v * (1 - s);
        const q = v * (1 - f * s);
        const t = v * (1 - (1 - f) * s);

        switch (i) {
            case 0: r = v; g = t; b = p; break;
            case 1: r = q; g = v; b = p; break;
            case 2: r = p; g = v; b = t; break;
            case 3: r = p; g = q; b = v; break;
            case 4: r = t; g = p; b = v; break;
            case 5: r = v; g = p; b = q; break;
        }
        return {
            r: Math.round(r * 255),
            g: Math.round(g * 255),
            b: Math.round(b * 255)
        };
    }

    static rgbToHsv(r, g, b) {
        r /= 255; g /= 255; b /= 255;
        const max = Math.max(r, g, b), min = Math.min(r, g, b);
        let h, s, v = max;
        const d = max - min;
        s = max === 0 ? 0 : d / max;

        if (max === min) {
            h = 0;
        } else {
            switch (max) {
                case r: h = (g - b) / d + (g < b ? 6 : 0); break;
                case g: h = (b - r) / d + 2; break;
                case b: h = (r - g) / d + 4; break;
            }
            h /= 6;
        }
        return {
            h: Math.round(h * 360) % 360,
            s: Math.max(0, Math.min(1, s)),
            v: Math.max(0, Math.min(1, v))
        };
    }
}

let colorWheelInstance = null;

function initColorWheel() {
    const canvas = document.getElementById("reinventedColorWheel");
    if (!canvas) return;

    colorWheelInstance = new ReinventedColorWheel(canvas, {
        onChange: ({ r, g, b, hex, h, s, v }) => {
            const hsvDisplay = document.getElementById("wheelHsvDisplay");
            if (hsvDisplay) {
                hsvDisplay.textContent = `H: ${h}° S: ${Math.round(s * 100)}% V: ${Math.round(v * 100)}%`;
            }
            syncRgbInputs(r, g, b, true);
        }
    });

    colorWheelInstance.setRgb(state.lights.color.r, state.lights.color.g, state.lights.color.b, false);
}

function syncRgbInputs(r, g, b, fromWheel = false, sendBackend = true) {
    state.lights.color.r = r;
    state.lights.color.g = g;
    state.lights.color.b = b;
    const hex = "#" + [r, g, b].map(x => x.toString(16).padStart(2, "0")).join("");
    state.lights.color.hex = hex;

    // Sync UI elements
    const sR = document.getElementById("sliderRed"); if (sR) sR.value = r;
    const iR = document.getElementById("inputRed"); if (iR) iR.value = r;
    const sG = document.getElementById("sliderGreen"); if (sG) sG.value = g;
    const iG = document.getElementById("inputGreen"); if (iG) iG.value = g;
    const sB = document.getElementById("sliderBlue"); if (sB) sB.value = b;
    const iB = document.getElementById("inputBlue"); if (iB) iB.value = b;

    const hexUpper = hex.toUpperCase();
    const hexClean = hexUpper.replace("#", "");
    const hexDisplay = document.getElementById("colorHexDisplay");
    if (hexDisplay) hexDisplay.textContent = hexUpper;

    const wheelHexInput = document.getElementById("wheelHexBadge");
    if (wheelHexInput && document.activeElement !== wheelHexInput) {
        wheelHexInput.value = hexClean;
    }

    const wheelHexWrapper = document.getElementById("wheelHexWrapper");
    if (wheelHexWrapper) {
        wheelHexWrapper.style.boxShadow = `0 0 12px ${hex}`;
        wheelHexWrapper.style.borderColor = hex;
    }

    const rgbDisplay = document.getElementById("wheelRgbDisplay");
    if (rgbDisplay) rgbDisplay.textContent = `RGB(${r}, ${g}, ${b})`;

    // Sync wheel puck if changed from outside (presets or sliders)
    if (!fromWheel && colorWheelInstance) {
        colorWheelInstance.setRgb(r, g, b, false);
        const hsvDisplay = document.getElementById("wheelHsvDisplay");
        if (hsvDisplay) {
            hsvDisplay.textContent = `H: ${colorWheelInstance.h}° S: ${Math.round(colorWheelInstance.s * 100)}% V: ${Math.round(colorWheelInstance.v * 100)}%`;
        }
    }

    // Reset preset active highlights
    document.querySelectorAll(".btn-preset").forEach(btn => btn.classList.remove("active"));
    if (r === 255 && g === 255 && b === 255) document.getElementById("presetWhite")?.classList.add("active");
    else if (r === 255 && g === 214 && b === 164) document.getElementById("presetWarmWhite")?.classList.add("active");
    else if (r === 255 && g === 0 && b === 0) document.getElementById("presetRed")?.classList.add("active");
    else if (r === 0 && g === 255 && b === 0) document.getElementById("presetGreen")?.classList.add("active");
    else if (r === 0 && g === 0 && b === 255) document.getElementById("presetBlue")?.classList.add("active");

    if (sendBackend && state.lights.power) {
        sendLightUpdate(false);
    }
}

function setMasterPower(on) {
    state.lights.power = !!on;
    if (state.lights.power) {
        lastUserInteraction = Date.now();
        ledAutoOffHandled = false;
    }
    const toggle = document.getElementById("masterPowerToggle");
    if (toggle) toggle.checked = state.lights.power;
    const btn = document.getElementById("masterPowerBtn");
    if (btn) {
        btn.classList.toggle("active", state.lights.power);
        const txt = document.getElementById("masterPowerBtnText");
        if (txt) txt.textContent = state.lights.power ? "POWER ON" : "POWER OFF";
    }
    const statusText = document.getElementById("powerStatusText");
    if (statusText) statusText.textContent = state.lights.power ? "STRIP ACTIVE" : "STRIP OFF";
    saveConfig();
    sendLightUpdate(true);
    showToast(state.lights.power ? "WS2812B Light ON" : "WS2812B Light OFF");
}

function setupLightListeners() {
    // Master Power Switch and Button
    const masterPower = document.getElementById("masterPowerToggle");
    const masterBtn = document.getElementById("masterPowerBtn");
    if (masterPower) {
        masterPower.checked = state.lights.power;
        masterPower.addEventListener("change", (e) => setMasterPower(e.target.checked));
    }
    if (masterBtn) {
        masterBtn.classList.toggle("active", state.lights.power);
        const txt = document.getElementById("masterPowerBtnText");
        if (txt) txt.textContent = state.lights.power ? "POWER ON" : "POWER OFF";
        masterBtn.addEventListener("click", () => setMasterPower(!state.lights.power));
    }
    const statusText = document.getElementById("powerStatusText");
    if (statusText) statusText.textContent = state.lights.power ? "STRIP ACTIVE" : "STRIP OFF";

    // Brightness Slider
    const brightnessSlider = document.getElementById("brightnessSlider");
    const brightnessBadge = document.getElementById("brightnessValBadge");
    brightnessSlider.value = state.lights.brightness_pct;
    brightnessBadge.textContent = `${state.lights.brightness_pct}%`;
    brightnessSlider.addEventListener("input", (e) => {
        const val = parseInt(e.target.value, 10);
        state.lights.brightness = val / 100.0;
        state.lights.brightness_pct = val;
        brightnessBadge.textContent = `${val}%`;
        sendLightUpdate();
    });

    // Initialize Precise Reinvented Color Wheel
    initColorWheel();

    // RGB Sliders & Numbers
    ["Red", "Green", "Blue"].forEach(channel => {
        const slider = document.getElementById(`slider${channel}`);
        const input = document.getElementById(`input${channel}`);

        slider?.addEventListener("input", (e) => {
            const val = parseInt(e.target.value, 10);
            input.value = val;
            const r = channel === "Red" ? val : state.lights.color.r;
            const g = channel === "Green" ? val : state.lights.color.g;
            const b = channel === "Blue" ? val : state.lights.color.b;
            syncRgbInputs(r, g, b, false);
        });

        input?.addEventListener("change", (e) => {
            let val = parseInt(e.target.value, 10);
            if (isNaN(val)) val = 0;
            val = Math.max(0, Math.min(255, val));
            input.value = val;
            slider.value = val;
            const r = channel === "Red" ? val : state.lights.color.r;
            const g = channel === "Green" ? val : state.lights.color.g;
            const b = channel === "Blue" ? val : state.lights.color.b;
            syncRgbInputs(r, g, b, false);
        });
    });

    // Presets (Left: White, Warm White, Yellow | Right: Red, Green, Blue)
    document.getElementById("presetWhite")?.addEventListener("click", () => syncRgbInputs(255, 255, 255, false));
    document.getElementById("presetWarmWhite")?.addEventListener("click", () => syncRgbInputs(255, 214, 164, false));
    document.getElementById("presetYellow")?.addEventListener("click", () => syncRgbInputs(255, 234, 0, false));
    document.getElementById("presetRed")?.addEventListener("click", () => syncRgbInputs(255, 0, 0, false));
    document.getElementById("presetGreen")?.addEventListener("click", () => syncRgbInputs(0, 255, 0, false));
    document.getElementById("presetBlue")?.addEventListener("click", () => syncRgbInputs(0, 0, 255, false));

    // Direct Editable Hex Input Listener (e.g. 00CC00)
    const hexInput = document.getElementById("wheelHexBadge");
    const hexWrapper = document.getElementById("wheelHexWrapper");
    if (hexInput) {
        if (hexWrapper) {
            hexWrapper.addEventListener("click", () => {
                hexInput.focus();
                hexInput.select();
            });
        }

        const applyHex = () => {
            let val = hexInput.value.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
            if (val.length === 3) {
                val = val[0] + val[0] + val[1] + val[1] + val[2] + val[2];
            }
            if (val.length === 6) {
                const r = parseInt(val.slice(0, 2), 16);
                const g = parseInt(val.slice(2, 4), 16);
                const b = parseInt(val.slice(4, 6), 16);
                if (!state.lights.power) setMasterPower(true);
                syncRgbInputs(r, g, b, false);
            }
        };

        hexInput.addEventListener("input", () => {
            let val = hexInput.value.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
            if (val.length === 6) {
                applyHex();
            }
        });

        hexInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                applyHex();
                hexInput.blur();
            }
        });

        hexInput.addEventListener("blur", () => {
            let val = hexInput.value.replace(/[^0-9A-Fa-f]/g, "").toUpperCase();
            if (val.length === 6 || val.length === 3) {
                applyHex();
            } else {
                hexInput.value = state.lights.color.hex.replace("#", "").toUpperCase();
            }
        });
    }

    // Effect Dropdown
    const effectSelect = document.getElementById("effectSelect");
    effectSelect.value = state.lights.effect;
    effectSelect.addEventListener("change", (e) => {
        state.lights.effect = e.target.value;
        showToast(`Effect set to: ${e.target.options[e.target.selectedIndex].text}`);
        sendLightUpdate();
    });

    // Effect Speed Slider
    const speedSlider = document.getElementById("speedSlider");
    const speedBadge = document.getElementById("speedValBadge");
    speedSlider.value = state.lights.speed;
    speedBadge.textContent = `${state.lights.speed} ms`;
    speedSlider.addEventListener("input", (e) => {
        const val = parseInt(e.target.value, 10);
        state.lights.speed = val;
        speedBadge.textContent = `${val} ms`;
        sendLightUpdate();
    });
}


// --- CAMERA CONTROLS ---

let fpsFrames = 0;
let lastFpsTime = performance.now();
let lastStreamFrameTime = performance.now();
let streamReconnectTimer = null;

function reconnectCameraStream(immediate = false) {
    const streamImg = document.getElementById("cameraStream");
    if (!streamImg || !state.camera.enabled) return;

    if (streamReconnectTimer) clearTimeout(streamReconnectTimer);
    streamReconnectTimer = setTimeout(() => {
        if (state.camera.enabled) {
            console.log("[Stream] Refreshing / reconnecting live camera MJPEG feed...");
            lastStreamFrameTime = performance.now();
            streamImg.src = `/stream?t=${Date.now()}`;
        }
    }, immediate ? 50 : 500);
}

function initFpsCounter() {
    const fpsBadge = document.getElementById("liveFpsBadge");
    const streamImg = document.getElementById("cameraStream");
    if (!streamImg) return;

    streamImg.addEventListener("load", () => {
        lastStreamFrameTime = performance.now();
        fpsFrames++;
        const now = performance.now();
        const delta = now - lastFpsTime;
        if (delta >= 1000) {
            const calculatedFps = Math.round((fpsFrames * 1000) / delta);
            if (fpsBadge) fpsBadge.textContent = `${calculatedFps} FPS`;
            fpsFrames = 0;
            lastFpsTime = now;
        }
    });

    // Auto-reconnect on image network error or dropped connection
    streamImg.addEventListener("error", () => {
        if (state.camera.enabled) {
            console.warn("[Stream] Image error event triggered, scheduling auto-reconnect...");
            reconnectCameraStream(false);
        }
    });

    // Watchdog: If no frames received for 4 seconds while enabled, automatically revive stream
    setInterval(() => {
        if (state.camera.enabled && (performance.now() - lastStreamFrameTime > 4000)) {
            console.warn("[Stream] Stream frame feed paused/stalled, auto-recovering...");
            lastStreamFrameTime = performance.now();
            reconnectCameraStream(true);
        }
    }, 2500);

    // Tap stream to manually force-refresh if ever needed
    streamImg.addEventListener("click", () => {
        if (state.camera.enabled) {
            reconnectCameraStream(true);
        }
    });
}

function applyViewportTransforms() {
    const stream = document.getElementById("cameraStream");
    if (!stream) return;

    const scale = state.camera.digital_zoom;
    const scaleX = state.camera.flip_h ? -scale : scale;
    const scaleY = state.camera.flip_v ? -scale : scale;
    stream.style.transform = `scale(${scaleX}, ${scaleY})`;

    const { brightness, contrast, saturation } = state.camera.filters;
    stream.style.filter = `brightness(${brightness}%) contrast(${contrast}%) saturate(${saturation}%)`;
}

// Available camera devices cache
let connectedCamerasList = [];

function getResolutionFriendlyLabel(resStr) {
    const parts = String(resStr).split("x").map(Number);
    if (parts.length !== 2 || isNaN(parts[0]) || isNaN(parts[1])) return resStr;
    const [w, h] = parts;
    if (w === 3840 && h === 2160) return "3840 x 2160 (4K Ultra HD 16:9)";
    if (w === 2560 && h === 1440) return "2560 x 1440 (2K QHD 16:9)";
    if (w === 1920 && h === 1080) return "1920 x 1080 (Full HD 1080p 16:9)";
    if (w === 1600 && h === 896) return "1600 x 896 (HD+ 16:9)";
    if (w === 1280 && h === 720) return "1280 x 720 (HD 720p 16:9)";
    if (w === 1024 && h === 576) return "1024 x 576 (WSVGA 16:9)";
    if (w === 960 && h === 720) return "960 x 720 (HD 4:3)";
    if (w === 800 && h === 600) return "800 x 600 (SVGA 4:3)";
    if (w === 800 && h === 448) return "800 x 448 (16:9)";
    if (w === 640 && h === 480) return "640 x 480 (VGA 4:3)";
    if (w === 640 && h === 360) return "640 x 360 (nHD 16:9)";
    if (w === 320 && h === 240) return "320 x 240 (QVGA 4:3)";
    return `${w} x ${h}`;
}

function populateCameraResolutions(resolutions) {
    const resSelect = document.getElementById("resolutionSelect");
    if (!resSelect) return;

    resSelect.innerHTML = "";
    const list = Array.isArray(resolutions) && resolutions.length > 0
        ? resolutions
        : ["1920x1080", "1280x720", "640x480"];

    list.forEach(res => {
        const opt = document.createElement("option");
        opt.value = res;
        opt.textContent = getResolutionFriendlyLabel(res);
        resSelect.appendChild(opt);
    });

    // Preferred resolution precedence: 1. saved state, 2. .env default, 3. first available (highest camera capability)
    const targetPreferred = state.camera.resolution || envDefaults.default_resolution || "1920x1080";
    let matched = false;
    for (let i = 0; i < resSelect.options.length; i++) {
        if (resSelect.options[i].value === targetPreferred) {
            resSelect.selectedIndex = i;
            matched = true;
            state.camera.resolution = targetPreferred;
            break;
        }
    }

    if (!matched) {
        // Camera does not support the preferred resolution (e.g., Logitech C922 cannot do 4K, or 720p cam cannot do 1080p)
        // Auto-select the camera's highest capability!
        resSelect.selectedIndex = 0;
        state.camera.resolution = resSelect.options[0]?.value || "1280x720";
        console.log(`[Camera Hardware] Clamped resolution to hardware maximum: ${state.camera.resolution}`);
    }
}

function applyCameraList(cameras) {
    const select = document.getElementById("cameraSelect");
    if (!select) return;
    select.innerHTML = "";
    connectedCamerasList = cameras || [];

    if (connectedCamerasList.length > 0) {
        let activeCam = null;
        connectedCamerasList.forEach(cam => {
            const opt = document.createElement("option");
            opt.value = cam.id;
            opt.textContent = `${cam.name} (${cam.type.toUpperCase()})`;
            if (cam.id === state.camera.active_id || cam.active) {
                opt.selected = true;
                state.camera.active_id = cam.id;
                activeCam = cam;
            }
            select.appendChild(opt);
        });
        if (!activeCam) {
            activeCam = connectedCamerasList[0];
            state.camera.active_id = activeCam.id;
            select.selectedIndex = 0;
        }
        const badge = document.getElementById("activeCamBadge");
        if (badge) badge.textContent = select.options[select.selectedIndex]?.text || "Camera Connected";

        // Dynamically populate resolution dropdown strictly from connected camera hardware data!
        populateCameraResolutions(activeCam.resolutions);
    } else {
        const opt = document.createElement("option");
        opt.value = "none";
        opt.textContent = "No camera detected";
        select.appendChild(opt);
        populateCameraResolutions([]);
    }
}

async function refreshCameraList() {
    try {
        const res = await fetch("/api/cameras");
        if (!res.ok) return;
        const data = await res.json();
        applyCameraList(data.cameras);
    } catch (e) {
        console.warn("Could not fetch camera list:", e);
    }
}

async function updateCameraSettings() {
    const [w, h] = state.camera.resolution.split("x").map(Number);
    try {
        await fetch("/api/camera/select", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                camera_id: state.camera.active_id,
                width: w,
                height: h,
                fps: state.camera.target_fps
            })
        });
        saveConfig();
        showToast(`Camera configured: ${w}x${h} @ ${state.camera.target_fps}fps`);

        // Refresh camera stream element with a timestamp to force new resolution stream
        const streamImg = document.getElementById("cameraStream");
        if (streamImg) {
            setTimeout(() => {
                streamImg.src = `/stream?t=${Date.now()}`;
            }, 350);
        }
    } catch (e) {
        console.error("Camera update failed:", e);
    }
}

const sendHardwareControl = debounce(async (control, value) => {
    try {
        await fetch("/api/camera/control", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                camera_id: state.camera.active_id,
                control: control,
                value: parseInt(value, 10)
            })
        });
    } catch (e) {
        console.warn("Control update failed:", e);
    }
}, 150);

function setupCameraListeners() {
    // In-Frame Mobile Camera Settings Overlay Controls
    const overlay = document.getElementById("cameraSettingsOverlay");
    const btnOpenOverlay = document.getElementById("btnToggleCamOverlay");
    const btnToolbarSettings = document.getElementById("btnToolbarSettings");
    const btnCloseOverlay = document.getElementById("btnCloseCamOverlay");

    function toggleOverlay(open) {
        if (!overlay) return;
        const willOpen = typeof open === "boolean" ? open : !overlay.classList.contains("open");
        overlay.classList.toggle("open", willOpen);
        reconnectCameraStream(false);
    }

    btnOpenOverlay?.addEventListener("click", () => toggleOverlay(true));
    btnToolbarSettings?.addEventListener("click", () => toggleOverlay(true));
    btnCloseOverlay?.addEventListener("click", () => toggleOverlay(false));

    window.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && overlay?.classList.contains("open")) {
            toggleOverlay(false);
        }
    });

    if (new URLSearchParams(window.location.search).has("open_cam")) {
        setTimeout(() => toggleOverlay(true), 200);
    }

    // Camera Select
    const select = document.getElementById("cameraSelect");
    select?.addEventListener("change", (e) => {
        state.camera.active_id = e.target.value;
        const selectedCam = connectedCamerasList.find(c => c.id === state.camera.active_id);
        if (selectedCam && selectedCam.resolutions) {
            populateCameraResolutions(selectedCam.resolutions);
        }
        updateCameraSettings();
        document.getElementById("activeCamBadge").textContent = select.options[select.selectedIndex]?.text;
    });

    document.getElementById("btnRefreshCams")?.addEventListener("click", () => {
        refreshCameraList();
        showToast("Scanning for USB and CSI cameras...");
    });

    // Camera Enable / Disable Toggle with state synchronization and rapid click protection
    let isTogglingCamera = false;
    const btnToggle = document.getElementById("btnToggleCamera");

    function updateCameraEnabledUI(enabled) {
        state.camera.enabled = !!enabled;
        if (btnToggle) {
            btnToggle.classList.toggle("danger", !state.camera.enabled);
            const span = btnToggle.querySelector("span");
            if (span) span.textContent = state.camera.enabled ? "Disable Cam" : "Enable Cam";
        }
        updateSnapshotButtonState();
    }

    // Expose for pollSystemStatus
    window.updateCameraEnabledUI = updateCameraEnabledUI;
    updateCameraEnabledUI(state.camera.enabled);

    btnToggle?.addEventListener("click", async () => {
        if (isTogglingCamera) return;
        isTogglingCamera = true;
        btnToggle.style.opacity = "0.5";

        const targetEnabled = !state.camera.enabled;
        try {
            const res = await fetch("/api/camera/toggle", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: targetEnabled })
            });
            if (res.ok) {
                const data = await res.json();
                const actual = (data.camera_enabled !== undefined) ? data.camera_enabled : targetEnabled;
                updateCameraEnabledUI(actual);
                showToast(actual ? "Camera enabled" : "Camera paused / sleep");
            } else {
                showToast("Failed to toggle camera state");
            }
        } catch (e) {
            console.error("Camera toggle failed:", e);
            showToast("Camera toggle network error");
        } finally {
            isTogglingCamera = false;
            btnToggle.style.opacity = "1";
        }

        const streamImg = document.getElementById("cameraStream");
        if (streamImg) {
            setTimeout(() => {
                streamImg.src = `/stream?t=${Date.now()}`;
            }, 300);
        }
    });

    // Resolution (Populated dynamically from connected camera hardware data)
    const resSelect = document.getElementById("resolutionSelect");
    resSelect?.addEventListener("change", (e) => {
        state.camera.resolution = e.target.value;
        updateCameraSettings();
    });

    const fpsSelect = document.getElementById("fpsSelect");
    fpsSelect.value = state.camera.target_fps.toString();
    fpsSelect.addEventListener("change", (e) => {
        state.camera.target_fps = parseInt(e.target.value, 10);
        updateCameraSettings();
    });

    // Digital Zoom
    const zoomSlider = document.getElementById("digitalZoomSlider");
    const zoomBadge = document.getElementById("digitalZoomBadge");
    zoomSlider.value = state.camera.digital_zoom;
    zoomBadge.textContent = `${state.camera.digital_zoom}x`;
    zoomSlider.addEventListener("input", (e) => {
        state.camera.digital_zoom = parseFloat(e.target.value);
        zoomBadge.textContent = `${state.camera.digital_zoom.toFixed(1)}x`;
        applyViewportTransforms();
        saveConfig();
    });

    // Flip Controls
    const btnFlipH = document.getElementById("btnFlipH");
    btnFlipH.classList.toggle("active", state.camera.flip_h);
    btnFlipH.addEventListener("click", () => {
        state.camera.flip_h = !state.camera.flip_h;
        btnFlipH.classList.toggle("active", state.camera.flip_h);
        applyViewportTransforms();
        saveConfig();
    });

    const btnFlipV = document.getElementById("btnFlipV");
    btnFlipV.classList.toggle("active", state.camera.flip_v);
    btnFlipV.addEventListener("click", () => {
        state.camera.flip_v = !state.camera.flip_v;
        btnFlipV.classList.toggle("active", state.camera.flip_v);
        applyViewportTransforms();
        saveConfig();
    });

    // Grid Overlay
    const btnGrid = document.getElementById("btnToggleGrid");
    const gridEl = document.getElementById("framingGrid");
    btnGrid.classList.toggle("active", state.camera.grid_active);
    gridEl.classList.toggle("active", state.camera.grid_active);
    btnGrid.addEventListener("click", () => {
        state.camera.grid_active = !state.camera.grid_active;
        btnGrid.classList.toggle("active", state.camera.grid_active);
        gridEl.classList.toggle("active", state.camera.grid_active);
        saveConfig();
    });

    // Fullscreen Viewport
    const btnFullscreen = document.getElementById("btnFullscreen");
    const viewportCard = document.getElementById("viewportCard");
    btnFullscreen.addEventListener("click", () => {
        if (!document.fullscreenElement) {
            viewportCard.requestFullscreen().catch(err => console.log(err));
        } else {
            document.exitFullscreen();
        }
    });

    // Unified Multi-Camera Snapshot (Combined Chamber + Device or Single Cam with 50px Twibbon)
    const btnSnapshot = document.getElementById("btnSnapshot");
    btnSnapshot?.addEventListener("click", () => {
        captureMultiCameraSnapshot();
    });

    // DirectShow / V4L2 Hardware Controls (Camera Control & Video Proc Amp tabs)
    setupDirectShowControls();
}

function bindControlPair(sliderId, numId, ctrlName, autoId = null, autoCtrlName = null, isExposure = false) {
    const slider = document.getElementById(sliderId);
    const num = document.getElementById(numId);
    const autoBox = autoId ? document.getElementById(autoId) : null;

    if (slider && num) {
        slider.addEventListener("input", (e) => {
            num.value = e.target.value;
            sendHardwareControl(ctrlName, e.target.value);
        });

        num.addEventListener("change", (e) => {
            let val = parseInt(e.target.value, 10);
            if (isNaN(val)) val = parseInt(slider.min, 10) || 0;
            slider.value = val;
            sendHardwareControl(ctrlName, val);
        });
    }

    if (autoBox && autoCtrlName && slider && num) {
        autoBox.addEventListener("change", (e) => {
            const isAuto = e.target.checked;
            slider.disabled = isAuto;
            num.disabled = isAuto;
            if (isExposure) {
                // V4L2 auto_exposure: 3=Auto aperture priority, 1=Manual
                sendHardwareControl(autoCtrlName, isAuto ? 3 : 1);
            } else {
                sendHardwareControl(autoCtrlName, isAuto ? 1 : 0);
            }
        });
    }
}

async function resetCameraDefaults() {
    try {
        showToast("Resetting camera controls to defaults...");
        const res = await fetch("/api/camera/reset_defaults", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ camera_id: state.camera.active_id })
        });
        const data = await res.json();
        if (data.camera && data.camera.controls) {
            syncControlsUIFromData(data.camera.controls);
        }
        showToast("Controls reset to defaults!");
    } catch (e) {
        showToast("Error resetting: " + e.message);
    }
}

function syncControlsUIFromData(controls) {
    if (!controls) return;
    function updateField(sliderId, numId, val) {
        const s = document.getElementById(sliderId);
        const n = document.getElementById(numId);
        if (s) s.value = val;
        if (n) n.value = val;
    }

    // Video Proc Amp
    if (controls.brightness?.value !== undefined) updateField("vpaBrightnessSlider", "vpaBrightnessNum", controls.brightness.value);
    if (controls.contrast?.value !== undefined) updateField("vpaContrastSlider", "vpaContrastNum", controls.contrast.value);
    if (controls.hue?.value !== undefined) updateField("vpaHueSlider", "vpaHueNum", controls.hue.value);
    if (controls.saturation?.value !== undefined) updateField("vpaSaturationSlider", "vpaSaturationNum", controls.saturation.value);
    if (controls.sharpness?.value !== undefined) updateField("vpaSharpnessSlider", "vpaSharpnessNum", controls.sharpness.value);
    if (controls.gamma?.value !== undefined) updateField("vpaGammaSlider", "vpaGammaNum", controls.gamma.value);
    if (controls.gain?.value !== undefined) updateField("vpaGainSlider", "vpaGainNum", controls.gain.value);
    if (controls.backlight_compensation?.value !== undefined) updateField("vpaBacklightSlider", "vpaBacklightNum", controls.backlight_compensation.value);

    if (controls.white_balance_temperature?.value !== undefined) {
        updateField("vpaWbSlider", "vpaWbNum", controls.white_balance_temperature.value);
    }
    if (controls.white_balance_automatic?.value !== undefined) {
        const wbAuto = document.getElementById("vpaWbAuto");
        const isAuto = !!controls.white_balance_automatic.value;
        if (wbAuto) wbAuto.checked = isAuto;
        const s = document.getElementById("vpaWbSlider");
        const n = document.getElementById("vpaWbNum");
        if (s) s.disabled = isAuto;
        if (n) n.disabled = isAuto;
    }
    if (controls.power_line_frequency?.value !== undefined) {
        const pl = document.getElementById("vpaPowerlineFreq");
        if (pl) pl.value = controls.power_line_frequency.value.toString();
    }

    // Camera Control
    if (controls.zoom_absolute?.value !== undefined) updateField("ctrlZoomSlider", "ctrlZoomNum", controls.zoom_absolute.value);
    if (controls.pan_absolute?.value !== undefined) updateField("ctrlPanSlider", "ctrlPanNum", controls.pan_absolute.value);
    if (controls.tilt_absolute?.value !== undefined) updateField("ctrlTiltSlider", "ctrlTiltNum", controls.tilt_absolute.value);

    if (controls.focus_absolute?.value !== undefined) {
        updateField("ctrlFocusSlider", "ctrlFocusNum", controls.focus_absolute.value);
    }
    if (controls.focus_automatic_continuous?.value !== undefined) {
        const fAuto = document.getElementById("ctrlFocusAuto");
        const isAuto = !!controls.focus_automatic_continuous.value;
        if (fAuto) fAuto.checked = isAuto;
        const s = document.getElementById("ctrlFocusSlider");
        const n = document.getElementById("ctrlFocusNum");
        if (s) s.disabled = isAuto;
        if (n) n.disabled = isAuto;
    }

    if (controls.exposure_time_absolute?.value !== undefined) {
        updateField("ctrlExpSlider", "ctrlExpNum", controls.exposure_time_absolute.value);
    }
    if (controls.auto_exposure?.value !== undefined) {
        const expAuto = document.getElementById("ctrlExpAuto");
        // 3 = auto, 1 = manual
        const isAuto = controls.auto_exposure.value === 3;
        if (expAuto) expAuto.checked = isAuto;
        const s = document.getElementById("ctrlExpSlider");
        const n = document.getElementById("ctrlExpNum");
        if (s) s.disabled = isAuto;
        if (n) n.disabled = isAuto;
    }

    if (controls.exposure_dynamic_framerate?.value !== undefined) {
        const llc = document.getElementById("ctrlLowLightComp");
        if (llc) llc.checked = !!controls.exposure_dynamic_framerate.value;
    }

    // Refresh visibility of circular reset arrows after syncing from backend
    window.updateResetButtonsState?.();
}

function setupDirectShowControls() {
    // 1. Tab Switching
    const tabBtns = document.querySelectorAll(".props-tab-btn");
    tabBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetId = btn.getAttribute("data-tab");
            tabBtns.forEach(b => b.classList.remove("active"));
            document.querySelectorAll(".props-tab-content").forEach(c => c.classList.remove("active"));
            btn.classList.add("active");
            document.getElementById(targetId)?.classList.add("active");
        });
    });

    const tabParam = new URLSearchParams(window.location.search).get("tab");
    if (tabParam === "stream") {
        document.getElementById("tabBtnStreamCfg")?.click();
    } else if (tabParam === "vpa") {
        document.getElementById("tabBtnVpa")?.click();
    }

    // 2. Camera Control Tab Bindings
    bindControlPair("ctrlZoomSlider", "ctrlZoomNum", "zoom");
    bindControlPair("ctrlFocusSlider", "ctrlFocusNum", "focus", "ctrlFocusAuto", "focus_auto", false);
    bindControlPair("ctrlExpSlider", "ctrlExpNum", "exposure", "ctrlExpAuto", "exposure_auto", true);
    bindControlPair("ctrlIrisSlider", "ctrlIrisNum", "aperture");
    bindControlPair("ctrlPanSlider", "ctrlPanNum", "pan");
    bindControlPair("ctrlTiltSlider", "ctrlTiltNum", "tilt");
    bindControlPair("ctrlRollSlider", "ctrlRollNum", "roll");

    // Low Light Compensation Checkbox
    const lowLightBox = document.getElementById("ctrlLowLightComp");
    lowLightBox?.addEventListener("change", (e) => {
        sendHardwareControl("low_light_comp", e.target.checked ? 1 : 0);
        showToast(e.target.checked ? "Low Light Comp ON (Variable FPS)" : "Low Light Comp OFF (Steady 30 FPS)");
    });

    // Reset Defaults buttons
    document.getElementById("btnResetCamCtrlDefaults")?.addEventListener("click", resetCameraDefaults);
    document.getElementById("btnResetVpaDefaults")?.addEventListener("click", resetCameraDefaults);

    // 3. Video Proc Amp Tab Bindings
    bindControlPair("vpaBrightnessSlider", "vpaBrightnessNum", "brightness");
    bindControlPair("vpaContrastSlider", "vpaContrastNum", "contrast");
    bindControlPair("vpaHueSlider", "vpaHueNum", "hue");
    bindControlPair("vpaSaturationSlider", "vpaSaturationNum", "saturation");
    bindControlPair("vpaSharpnessSlider", "vpaSharpnessNum", "sharpness");
    bindControlPair("vpaGammaSlider", "vpaGammaNum", "gamma");
    bindControlPair("vpaWbSlider", "vpaWbNum", "white_balance", "vpaWbAuto", "white_balance_auto", false);
    bindControlPair("vpaBacklightSlider", "vpaBacklightNum", "backlight_comp");
    bindControlPair("vpaGainSlider", "vpaGainNum", "gain");

    // ColorEnable Checkbox
    const colorEnableBox = document.getElementById("vpaColorEnable");
    colorEnableBox?.addEventListener("change", (e) => {
        const satVal = e.target.checked ? 128 : 0;
        document.getElementById("vpaSaturationSlider").value = satVal;
        document.getElementById("vpaSaturationNum").value = satVal;
        sendHardwareControl("saturation", satVal);
    });

    // PowerLine Frequency Dropdown (Default from .env: 50Hz)
    const powerlineSelect = document.getElementById("vpaPowerlineFreq");
    if (powerlineSelect && envDefaults.anti_flicker_code !== undefined) {
        powerlineSelect.value = envDefaults.anti_flicker_code.toString();
    }
    powerlineSelect?.addEventListener("change", (e) => {
        sendHardwareControl("powerline_freq", e.target.value);
        showToast(`Anti-Flicker: ${powerlineSelect.options[powerlineSelect.selectedIndex].text}`);
    });
}


// --- STATUS POLLING & HEARTBEAT ---

let initialControlsSynced = false;
let initialLightsSynced = false;

async function pollSystemStatus() {
    const start = performance.now();
    try {
        const res = await fetch("/api/status");
        const data = await res.json();
        const latency = Math.round(performance.now() - start);

        const statusPill = document.getElementById("statusPill");
        const statusDot = document.getElementById("statusDot");
        const statusText = document.getElementById("statusText");
        const latencyText = document.getElementById("latencyText");
        const statusDotMobile = document.getElementById("statusDotMobile");
        const statusTextMobile = document.getElementById("statusTextMobile");

        if (data.status === "ok" && data.system) {
            state.system.online = true;
            statusDot.className = "pill-dot online";
            statusText.textContent = "CONNECTED";
            latencyText.textContent = `${latency}ms`;

            if (statusDotMobile) statusDotMobile.className = "pill-dot online";
            if (statusTextMobile) statusTextMobile.textContent = "LIVE";

            if (data.system.cpu_temp_c !== null) {
                const tempStr = `${data.system.cpu_temp_c}°C`;
                const el = document.getElementById("statTemp");
                const elDr = document.getElementById("statTempDrawer");
                if (el) el.textContent = tempStr;
                if (elDr) elDr.textContent = tempStr;
            }
            if (data.system.ram_free_mb !== null) {
                const ramStr = `${data.system.ram_free_mb} MB`;
                const el = document.getElementById("statRam");
                const elDr = document.getElementById("statRamDrawer");
                if (el) el.textContent = ramStr;
                if (elDr) elDr.textContent = ramStr;
            }
            if (data.system.load_avg) {
                const cpuStr = `${data.system.load_avg[0].toFixed(2)}`;
                const el = document.getElementById("statCpu");
                const elDr = document.getElementById("statCpuDrawer");
                if (el) el.textContent = cpuStr;
                if (elDr) elDr.textContent = cpuStr;
            }

            // Sync camera controls from hardware
            if (!initialControlsSynced && data.camera && data.camera.controls) {
                syncControlsUIFromData(data.camera.controls);
                initialControlsSynced = true;
            }

            // Sync camera enabled hardware state
            if (data.camera && data.camera.camera_enabled !== undefined) {
                if (window.updateCameraEnabledUI && state.camera.enabled !== data.camera.camera_enabled) {
                    window.updateCameraEnabledUI(data.camera.camera_enabled);
                }
            }

            // Sync lights initial hardware power
            if (!initialLightsSynced && data.lights) {
                if (data.lights.power !== undefined) {
                    state.lights.power = data.lights.power;
                    const toggle = document.getElementById("masterPowerToggle");
                    if (toggle) toggle.checked = state.lights.power;
                    const btn = document.getElementById("masterPowerBtn");
                    if (btn) {
                        btn.classList.toggle("active", state.lights.power);
                        const txt = document.getElementById("masterPowerBtnText");
                        if (txt) txt.textContent = state.lights.power ? "POWER ON" : "POWER OFF";
                    }
                    const statusText = document.getElementById("powerStatusText");
                    if (statusText) statusText.textContent = state.lights.power ? "STRIP ACTIVE" : "STRIP OFF";
                }
                initialLightsSynced = true;
            }
        } else {
            state.system.online = false;
            statusDot.className = "pill-dot offline";
            statusText.textContent = "OFFLINE";
            latencyText.textContent = "--";
            if (statusDotMobile) statusDotMobile.className = "pill-dot offline";
            if (statusTextMobile) statusTextMobile.textContent = "OFFLINE";
        }
    } catch (e) {
        const dot = document.getElementById("statusDot");
        const txt = document.getElementById("statusText");
        const lat = document.getElementById("latencyText");
        if (dot) dot.className = "pill-dot offline";
        if (txt) txt.textContent = "DISCONNECTED";
        if (lat) lat.textContent = "--";
        const dotM = document.getElementById("statusDotMobile");
        const txtM = document.getElementById("statusTextMobile");
        if (dotM) dotM.className = "pill-dot offline";
        if (txtM) txtM.textContent = "DISCONNECTED";
    }
}


// --- INACTIVITY & HEARTBEAT TRACKING ---
let lastUserInteraction = Date.now();
let userActiveSinceLastHeartbeat = true;
let ledAutoOffHandled = false;
let systemSleepHandled = false;

function setupInactivityHeartbeat() {
    const events = ["mousemove", "keydown", "click", "touchstart", "scroll"];
    events.forEach(evt => {
        window.addEventListener(evt, () => {
            lastUserInteraction = Date.now();
            userActiveSinceLastHeartbeat = true;
            ledAutoOffHandled = false;
            systemSleepHandled = false;
        }, { passive: true });
    });

    const timerEl = document.getElementById("statIdleTimer");
    const timerDrawerEl = document.getElementById("statIdleTimerDrawer");
    const labelEl = document.getElementById("statIdleLabel");

    const updateTimerText = (formattedTime, isWarning, labelText = "Sleep:") => {
        if (timerEl) {
            timerEl.textContent = formattedTime;
            timerEl.style.color = isWarning ? "var(--neon-red)" : "";
        }
        if (timerDrawerEl) {
            timerDrawerEl.textContent = formattedTime;
            timerDrawerEl.style.color = isWarning ? "var(--neon-red)" : "";
        }
        if (labelEl) {
            labelEl.textContent = labelText;
        }
    };

    const ledTimeout = envDefaults.led_auto_off || 120;
    const systemSleepTimeout = envDefaults.inactivity_time || 600;

    // Periodic heartbeat every 20 seconds while user is active
    setInterval(async () => {
        if (userActiveSinceLastHeartbeat) {
            try {
                const res = await fetch("/api/heartbeat", { method: "POST" });
                if (res.ok) {
                    const data = await res.json();
                    userActiveSinceLastHeartbeat = false;
                }
            } catch (e) {
                // Ignore transient network errors
            }
        }
    }, 20000);

    // Update countdown timer display every 1 second
    setInterval(() => {
        const elapsedSec = Math.floor((Date.now() - lastUserInteraction) / 1000);

        // Stage 1: WS2812B LED Auto-Off after 2 minutes (120s) - Camera stays ON
        if (elapsedSec >= ledTimeout && state.lights.power && !ledAutoOffHandled) {
            ledAutoOffHandled = true;
            setMasterPower(false);
            showToast("LED Auto-Off: Lights shut down due to 2m inactivity (Camera remains active)");
        }

        // Stage 2: Full System Shutdown after 10 minutes (600s) - Camera & system powered down
        if (elapsedSec >= systemSleepTimeout && (state.camera.enabled || state.lights.power) && !systemSleepHandled) {
            systemSleepHandled = true;
            setMasterPower(false);
            if (state.camera.enabled) {
                state.camera.enabled = false;
                const btnCamToggle = document.getElementById("btnToggleCamera");
                if (btnCamToggle) {
                    btnCamToggle.classList.add("danger");
                    btnCamToggle.querySelector("span").textContent = "Enable Cam";
                }
                fetch("/api/camera/toggle", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ enabled: false })
                });
            }
            showToast("Inactivity Timeout: System & camera powered down after 10m inactivity.");
        }

        // Display countdown: If lights are currently ON and haven't reached 2m, show LED countdown
        if (state.lights.power && elapsedSec < ledTimeout) {
            const remainingLed = Math.max(0, ledTimeout - elapsedSec);
            const m = Math.floor(remainingLed / 60);
            const s = remainingLed % 60;
            const formatted = `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
            updateTimerText(formatted, remainingLed <= 30, "LED Off:");
        } else {
            // Otherwise show system sleep countdown (10m)
            const remainingSys = Math.max(0, systemSleepTimeout - elapsedSec);
            const m = Math.floor(remainingSys / 60);
            const s = remainingSys % 60;
            const formatted = `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
            updateTimerText(formatted, remainingSys <= 60, "Sleep:");
        }
    }, 1000);
}



// --- MOBILE LED OFF-CANVAS HAMBURGER DRAWER ---
function setupMobileLedDrawer() {
    const btnHamburger = document.getElementById("btnHamburgerLed");
    const lightsPanel = document.getElementById("lightsPanel");
    const backdrop = document.getElementById("mobileLedBackdrop");
    const btnClose = document.getElementById("btnCloseLedPanel");

    function toggleMobileDrawer(open) {
        if (!lightsPanel) return;
        const willOpen = typeof open === "boolean" ? open : !lightsPanel.classList.contains("mobile-open");
        lightsPanel.classList.toggle("mobile-open", willOpen);
        if (backdrop) {
            backdrop.classList.toggle("active", willOpen);
        }
        reconnectCameraStream(false);
    }

    if (btnHamburger) {
        btnHamburger.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            toggleMobileDrawer(true);
        });
    }

    if (btnClose) {
        btnClose.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            toggleMobileDrawer(false);
        });
    }

    if (backdrop) {
        backdrop.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            toggleMobileDrawer(false);
        });
    }

    if (lightsPanel) {
        lightsPanel.addEventListener("click", (e) => {
            e.stopPropagation();
        });
    }

    window.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && lightsPanel?.classList.contains("mobile-open")) {
            toggleMobileDrawer(false);
        }
    });

    if (new URLSearchParams(window.location.search).has("open_led")) {
        setTimeout(() => toggleMobileDrawer(true), 200);
    }
}


// --- OCR SECURITY PASSWORD AUTHENTICATION ---
function setupOcrAuth() {
    const btnOcr = document.getElementById("btnOcrCapture");
    const modal = document.getElementById("ocrAuthModal");
    const form = document.getElementById("ocrAuthForm");
    const pwdInput = document.getElementById("ocrPasswordInput");
    const btnTogglePwd = document.getElementById("btnToggleOcrPwdVisibility");
    const eyeIcon = document.getElementById("ocrEyeIcon");
    const errorMsg = document.getElementById("ocrErrorMsg");
    const btnCancel = document.getElementById("btnCloseOcrModal");
    const btnClose = document.getElementById("btnCancelOcrModal");
    const btnSubmit = document.getElementById("btnSubmitOcrPassword");
    const submitText = document.getElementById("ocrSubmitText");
    const spinner = document.getElementById("ocrSpinner");
    const modalContent = modal?.querySelector(".ocr-modal-card");

    // Check if already authenticated in this session
    if (sessionStorage.getItem("lumen_ocr_authenticated") === "true") {
        btnOcr?.classList.add("unlocked");
        btnOcr?.setAttribute("title", "OCR Feature Unlocked (Active)");
    }

    function openModal() {
        if (!modal) return;
        if (sessionStorage.getItem("lumen_ocr_authenticated") === "true") {
            showToast("🔓 OCR Access is already unlocked for this session");
            return;
        }
        if (errorMsg) {
            errorMsg.textContent = "";
            errorMsg.classList.remove("visible");
        }
        if (pwdInput) {
            pwdInput.value = "";
            pwdInput.type = "password";
        }
        modal.classList.add("active");
        setTimeout(() => pwdInput?.focus(), 150);
    }

    function closeModal() {
        if (!modal) return;
        modal.classList.remove("active");
        if (errorMsg) errorMsg.classList.remove("visible");
    }

    btnOcr?.addEventListener("click", (e) => {
        e.preventDefault();
        openModal();
    });

    btnCancel?.addEventListener("click", closeModal);
    btnClose?.addEventListener("click", closeModal);

    modal?.addEventListener("click", (e) => {
        if (e.target === modal) closeModal();
    });

    window.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && modal?.classList.contains("active")) {
            closeModal();
        }
    });

    // Toggle password reveal / mask
    btnTogglePwd?.addEventListener("click", () => {
        if (!pwdInput) return;
        const isPwd = pwdInput.type === "password";
        pwdInput.type = isPwd ? "text" : "password";
        if (eyeIcon) {
            eyeIcon.innerHTML = isPwd
                ? '<path d="M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.43-4.75-1.73-4.39-6-7.5-11-7.5-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78l3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z"/>'
                : '<path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z"/>';
        }
    });

    // Form submit verification
    form?.addEventListener("submit", async (e) => {
        e.preventDefault();
        const entered = (pwdInput?.value || "").trim();
        if (!entered) {
            if (errorMsg) {
                errorMsg.textContent = "Please enter the password.";
                errorMsg.classList.add("visible");
            }
            pwdInput?.focus();
            return;
        }

        // Set loading state
        if (btnSubmit) btnSubmit.disabled = true;
        if (submitText) submitText.style.display = "none";
        if (spinner) spinner.style.display = "inline-block";
        if (errorMsg) errorMsg.classList.remove("visible");

        try {
            const res = await fetch("/api/ocr/verify", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password: entered })
            });
            const data = await res.json();

            if (res.ok && data.success) {
                sessionStorage.setItem("lumen_ocr_authenticated", "true");
                btnOcr?.classList.add("unlocked");
                btnOcr?.setAttribute("title", "OCR Feature Unlocked (Active)");
                closeModal();
                showToast("🔓 OCR Access Granted! Text recognition module unlocked.");
            } else {
                if (errorMsg) {
                    errorMsg.textContent = data.error || "Invalid password. Access denied.";
                    errorMsg.classList.add("visible");
                }
                if (modalContent) {
                    modalContent.classList.add("ocr-shake");
                    setTimeout(() => modalContent.classList.remove("ocr-shake"), 400);
                }
                pwdInput?.select();
            }
        } catch (err) {
            if (errorMsg) {
                errorMsg.textContent = "Network error verifying password. Please try again.";
                errorMsg.classList.add("visible");
            }
        } finally {
            if (btnSubmit) btnSubmit.disabled = false;
            if (submitText) submitText.style.display = "inline";
            if (spinner) spinner.style.display = "none";
        }
    });

    if (new URLSearchParams(window.location.search).has("open_ocr")) {
        setTimeout(() => openModal(), 200);
    }
}


// --- RESET TO SERVER DEFAULTS WITH POP-UP CONFIRMATION ---
function setupResetToDefaults() {
    const btnDesktop = document.getElementById("btnResetAllDefaults");
    const btnMobile = document.getElementById("btnResetAllDefaultsMobile");
    const modal = document.getElementById("confirmResetModal");
    const btnCancel = document.getElementById("btnCancelResetModal");
    const btnClose = document.getElementById("btnCancelResetClose");
    const btnConfirm = document.getElementById("btnConfirmResetSubmit");
    const spinner = document.getElementById("resetSpinner");

    function openModal() {
        if (!modal) return;
        modal.classList.add("active");
    }

    function closeModal() {
        if (!modal) return;
        modal.classList.remove("active");
    }

    btnDesktop?.addEventListener("click", openModal);
    btnMobile?.addEventListener("click", openModal);
    btnCancel?.addEventListener("click", closeModal);
    btnClose?.addEventListener("click", closeModal);

    modal?.addEventListener("click", (e) => {
        if (e.target === modal) closeModal();
    });

    window.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && modal?.classList.contains("active")) {
            closeModal();
        }
    });

    btnConfirm?.addEventListener("click", async () => {
        if (btnConfirm) btnConfirm.disabled = true;
        if (spinner) spinner.style.display = "inline-block";

        try {
            // Reset hardware controls on Pi camera to factory defaults
            if (state.camera.active_id) {
                await fetch("/api/camera/reset_defaults", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ camera_id: state.camera.active_id })
                }).catch(() => { });
            }
        } catch (e) {
            console.warn("Could not reset camera hardware defaults:", e);
        }

        // Wipe all browser storage and cache
        try {
            localStorage.removeItem("lumen_zero_user_config_v2");
            localStorage.clear();
            sessionStorage.clear();
            console.log("[Reset] All browser localStorage & sessionStorage caches purged.");
        } catch (e) {
            console.warn("[Reset] Storage purge warning:", e);
        }

        showToast("🧹 Browser cache purged! Restoring server defaults...");

        // Reload page to re-initialize completely with fresh server defaults from .env
        setTimeout(() => {
            window.location.href = window.location.pathname;
        }, 400);
    });
}

// ==========================================================================
// MULTI-CAMERA & LOCAL DEVICE CAMERA (BROWSER WEBCAM) ENGINE
// ==========================================================================

function updateSnapshotButtonState() {
    const btnSnapshot = document.getElementById("btnSnapshot");
    const lblSnapshotText = document.getElementById("lblSnapshotText");
    if (!btnSnapshot) return;
    const canSnapshot = state.camera.enabled || (state.deviceCamera && state.deviceCamera.active);
    btnSnapshot.disabled = !canSnapshot;
    btnSnapshot.classList.toggle("disabled", !canSnapshot);

    if (state.camera.enabled && state.deviceCamera?.active) {
        btnSnapshot.title = "Capture combined dual-camera snapshot (Chamber + Device)";
        if (lblSnapshotText) lblSnapshotText.textContent = "Catch It!";
    } else if (state.camera.enabled) {
        btnSnapshot.title = "Capture Chamber camera snapshot";
        if (lblSnapshotText) lblSnapshotText.textContent = "Catch It!";
    } else if (state.deviceCamera?.active) {
        btnSnapshot.title = "Capture Device camera snapshot";
        if (lblSnapshotText) lblSnapshotText.textContent = "Catch It!";
    } else {
        btnSnapshot.title = "Cameras are disabled (enable camera to capture snapshot)";
        if (lblSnapshotText) lblSnapshotText.textContent = "Catch It!";
    }
}

function applyDeviceViewportTransforms() {
    const video = document.getElementById("deviceCameraVideo");
    if (!video) return;

    const rot = state.deviceCamera.rotation || 0;
    const scale = state.deviceCamera.digital_zoom || 1.0;
    const scaleX = (state.deviceCamera.flip_h ? -scale : scale);
    const scaleY = (state.deviceCamera.flip_v ? -scale : scale);

    video.style.transformOrigin = "center center";
    video.style.transform = `rotate(${rot}deg) scale(${scaleX}, ${scaleY})`;
    video.style.objectFit = state.deviceCamera.fit_mode || "contain";

    const filters = state.deviceCamera.filters || {};
    const brightness = filters.brightness ?? 100;
    const contrast = filters.contrast ?? 100;
    const saturation = filters.saturation ?? 100;
    const sharpness = filters.sharpness ?? 100;

    let filterStr = `brightness(${brightness}%) contrast(${contrast}%) saturate(${saturation}%)`;
    if (sharpness && sharpness !== 100) {
        const sharpFactor = (sharpness - 100) / 100;
        filterStr += ` contrast(${100 + sharpFactor * 25}%)`;
    }
    video.style.filter = filterStr;
}

async function applyDeviceHardwareConstraints() {
    if (!state.deviceCamera.stream) return;
    const tracks = state.deviceCamera.stream.getVideoTracks();
    if (!tracks || tracks.length === 0) return;
    const track = tracks[0];

    try {
        const capabilities = track.getCapabilities ? track.getCapabilities() : {};
        const advanced = [];

        // Hardware focus mode & distance
        if (capabilities.focusMode) {
            const mode = state.deviceCamera.focus_mode === "manual" ? "manual" : "continuous";
            if (capabilities.focusMode.includes(mode)) {
                const adv = { focusMode: mode };
                if (mode === "manual" && capabilities.focusDistance && state.deviceCamera.focus_distance !== undefined) {
                    const min = capabilities.focusDistance.min || 0;
                    const max = capabilities.focusDistance.max || 100;
                    const distVal = min + (state.deviceCamera.focus_distance / 100) * (max - min);
                    adv.focusDistance = distVal;
                }
                advanced.push(adv);
            }
        }

        if (advanced.length > 0) {
            await track.applyConstraints({ advanced });
            console.log("[DeviceCam] Hardware constraints applied:", advanced);
        }
    } catch (e) {
        console.warn("[DeviceCam] Hardware constraints not supported by device/browser:", e);
    }
}

async function enumerateDeviceCameras() {
    try {
        const select = document.getElementById("deviceSourceSelect");
        if (!select || !navigator.mediaDevices?.enumerateDevices) return;
        const devices = await navigator.mediaDevices.enumerateDevices();
        const videoInputs = devices.filter(d => d.kind === "videoinput");

        select.innerHTML = "";
        videoInputs.forEach((dev, idx) => {
            const opt = document.createElement("option");
            opt.value = dev.deviceId;
            opt.textContent = dev.label || `Camera ${idx + 1}`;
            if (dev.deviceId === state.deviceCamera.deviceId) {
                opt.selected = true;
            }
            select.appendChild(opt);
        });

        if (videoInputs.length > 0 && !state.deviceCamera.deviceId) {
            state.deviceCamera.deviceId = videoInputs[0].deviceId;
        }

        const badge = document.getElementById("deviceCamBadge");
        if (badge && select.options[select.selectedIndex]) {
            badge.textContent = select.options[select.selectedIndex].text;
        }
    } catch (e) {
        console.warn("Could not enumerate device cameras:", e);
    }
}

let isDeviceCamStarting = false;

async function startDeviceCameraStream(preserveStateOnError = false) {
    if (isDeviceCamStarting) return;
    isDeviceCamStarting = true;

    const stage = document.getElementById("multiCameraStage");
    const slotDevice = document.getElementById("slotDevice");
    const video = document.getElementById("deviceCameraVideo");
    const standby = document.getElementById("deviceStandbyOverlay");
    const chk = document.getElementById("chkEnableDeviceCam");

    // 1. Fully release previous stream & tracks so mobile OS camera HAL unbinds hardware
    if (state.deviceCamera.stream) {
        try {
            state.deviceCamera.stream.getTracks().forEach(track => {
                try { track.stop(); } catch (_) { }
            });
        } catch (_) { }
        state.deviceCamera.stream = null;
    }
    if (video) {
        video.srcObject = null;
    }

    // 2. Allow mobile OS camera driver 100ms to cleanly release sensor mutex
    await new Promise(r => setTimeout(r, 100));

    try {
        showToast("Accessing device camera...");
        const [reqW, reqH] = (state.deviceCamera.resolution || "1920x1080").split("x").map(Number);
        const targetDeviceId = state.deviceCamera.deviceId;

        const baseVideoConstraints = {
            width: { ideal: reqW || 1920 },
            height: { ideal: reqH || 1080 }
        };

        let stream = null;
        let lastError = null;

        // Attempt 1: Exact targetDeviceId
        if (targetDeviceId && targetDeviceId !== "default") {
            try {
                stream = await navigator.mediaDevices.getUserMedia({
                    video: { ...baseVideoConstraints, deviceId: { exact: targetDeviceId } },
                    audio: false
                });
            } catch (err1) {
                console.warn("[DeviceCam] Exact deviceId request failed, trying ideal deviceId:", err1);
                lastError = err1;
                await new Promise(r => setTimeout(r, 120));
            }
        }

        // Attempt 2: Ideal targetDeviceId (gracefully relaxes overconstrained attributes on Android/iOS)
        if (!stream && targetDeviceId && targetDeviceId !== "default") {
            try {
                stream = await navigator.mediaDevices.getUserMedia({
                    video: { ...baseVideoConstraints, deviceId: { ideal: targetDeviceId } },
                    audio: false
                });
            } catch (err2) {
                console.warn("[DeviceCam] Ideal deviceId request failed, trying fallback:", err2);
                lastError = err2;
                await new Promise(r => setTimeout(r, 120));
            }
        }

        // Attempt 3: General video input fallback if specific sensor constraint threw
        if (!stream) {
            try {
                stream = await navigator.mediaDevices.getUserMedia({
                    video: baseVideoConstraints,
                    audio: false
                });
            } catch (err3) {
                throw lastError || err3;
            }
        }

        state.deviceCamera.stream = stream;
        state.deviceCamera.enabled = true;
        state.deviceCamera.active = true;

        // Synchronize active deviceId from opened track settings
        const videoTrack = stream.getVideoTracks()[0];
        if (videoTrack) {
            const settings = videoTrack.getSettings ? videoTrack.getSettings() : {};
            if (settings.deviceId) {
                state.deviceCamera.deviceId = settings.deviceId;
            }
        }

        if (video) {
            video.srcObject = stream;
            await video.play().catch(() => { });
        }

        if (chk) chk.checked = true;
        if (slotDevice) slotDevice.classList.remove("hidden");
        if (stage) stage.classList.add("dual-active");
        if (standby) standby.classList.add("hidden");

        const btnToggleStream = document.getElementById("btnToggleDeviceCamStream");
        if (btnToggleStream) {
            btnToggleStream.classList.remove("danger");
            const span = btnToggleStream.querySelector("span");
            if (span) span.textContent = "Disable Cam";
        }

        await enumerateDeviceCameras();
        applyDeviceViewportTransforms();
        await applyDeviceHardwareConstraints();
        updateSnapshotButtonState();
        showToast("Device camera activated successfully!");
    } catch (err) {
        console.error("Device camera access error:", err);
        if (!preserveStateOnError) {
            state.deviceCamera.enabled = false;
            state.deviceCamera.active = false;
            if (chk) chk.checked = false;
            if (slotDevice) slotDevice.classList.add("hidden");
            if (stage) stage.classList.remove("dual-active");
            if (standby) standby.classList.remove("hidden");
            updateSnapshotButtonState();
        }
        showToast("Camera access rejected or unavailable: " + (err.name || err.message));
    } finally {
        isDeviceCamStarting = false;
    }
}

function stopDeviceCameraStream() {
    const stage = document.getElementById("multiCameraStage");
    const slotDevice = document.getElementById("slotDevice");
    const video = document.getElementById("deviceCameraVideo");
    const standby = document.getElementById("deviceStandbyOverlay");
    const chk = document.getElementById("chkEnableDeviceCam");

    if (state.deviceCamera.stream) {
        state.deviceCamera.stream.getTracks().forEach(track => track.stop());
        state.deviceCamera.stream = null;
    }
    state.deviceCamera.enabled = false;
    state.deviceCamera.active = false;

    if (video) {
        video.srcObject = null;
    }
    if (chk) chk.checked = false;
    if (slotDevice) slotDevice.classList.add("hidden");
    if (stage) stage.classList.remove("dual-active");
    if (standby) standby.classList.remove("hidden");

    updateSnapshotButtonState();
    showToast("Device camera turned off");
}

function setupDeviceCameraManager() {
    const chk = document.getElementById("chkEnableDeviceCam");
    const overlay = document.getElementById("deviceSettingsOverlay");
    const btnOpenOverlay = document.getElementById("btnToggleDeviceOverlay");
    const btnToolbarSettings = document.getElementById("btnDeviceSettings");
    const btnCloseOverlay = document.getElementById("btnCloseDeviceOverlay");

    chk?.addEventListener("change", (e) => {
        if (e.target.checked) {
            startDeviceCameraStream();
        } else {
            stopDeviceCameraStream();
        }
    });

    function toggleDeviceOverlay(open) {
        if (!overlay) return;
        const willOpen = typeof open === "boolean" ? open : !overlay.classList.contains("open");
        overlay.classList.toggle("open", willOpen);
    }

    btnOpenOverlay?.addEventListener("click", () => toggleDeviceOverlay(true));
    btnToolbarSettings?.addEventListener("click", () => toggleDeviceOverlay(true));
    btnCloseOverlay?.addEventListener("click", () => toggleDeviceOverlay(false));

    // Device Stream Toggle (Mute / Unmute video tracks)
    const btnToggleStream = document.getElementById("btnToggleDeviceCamStream");
    btnToggleStream?.addEventListener("click", () => {
        if (!state.deviceCamera.stream) {
            startDeviceCameraStream();
            return;
        }
        const tracks = state.deviceCamera.stream.getVideoTracks();
        if (tracks.length > 0) {
            const nextState = !tracks[0].enabled;
            tracks.forEach(t => t.enabled = nextState);
            state.deviceCamera.active = nextState;
            btnToggleStream.classList.toggle("danger", !nextState);
            const span = btnToggleStream.querySelector("span");
            if (span) span.textContent = nextState ? "Disable Cam" : "Enable Cam";

            const standby = document.getElementById("deviceStandbyOverlay");
            if (standby) standby.classList.toggle("hidden", nextState);

            updateSnapshotButtonState();
            showToast(nextState ? "Device camera resumed" : "Device camera paused");
        }
    });

    // Rotation Controls (Dedicated Toolbar Button & Overlay Select)
    const btnRotate = document.getElementById("btnDeviceRotate");
    const lblRotate = document.getElementById("lblDeviceRotate");
    const rotateSelect = document.getElementById("deviceRotateSelect");

    function updateRotateUI(deg) {
        state.deviceCamera.rotation = deg;
        if (rotateSelect) rotateSelect.value = String(deg);
        if (lblRotate) lblRotate.textContent = deg === 0 ? "Rotate" : `Rotate (${deg}°)`;
        btnRotate?.classList.toggle("active", deg !== 0);
        applyDeviceViewportTransforms();
        saveConfig();
    }

    if (rotateSelect) {
        rotateSelect.value = String(state.deviceCamera.rotation || 0);
        rotateSelect.addEventListener("change", (e) => {
            const deg = parseInt(e.target.value, 10) || 0;
            updateRotateUI(deg);
            showToast(`Device camera rotated to ${deg}°`);
        });
    }

    btnRotate?.addEventListener("click", () => {
        const currentDeg = state.deviceCamera.rotation || 0;
        const nextDeg = (currentDeg + 90) % 360;
        updateRotateUI(nextDeg);
        showToast(`Device camera rotated to ${nextDeg}°`);
    });

    // Framing / Fit Mode Control (Fit vs Fill)
    const fitSelect = document.getElementById("deviceFitSelect");
    if (fitSelect) {
        fitSelect.value = state.deviceCamera.fit_mode || envDefaults.default_device_cam_fit || "cover";
        fitSelect.addEventListener("change", (e) => {
            state.deviceCamera.fit_mode = e.target.value;
            applyDeviceViewportTransforms();
            saveConfig();
            showToast(`Framing mode: ${e.target.value === "cover" ? "Fill (Crop to 16:9)" : "Fit (No Crop — Full View)"}`);
        });
    }

    // Flip Controls
    const btnFlipH = document.getElementById("btnDeviceFlipH");
    btnFlipH?.classList.toggle("active", state.deviceCamera.flip_h);
    btnFlipH?.addEventListener("click", () => {
        state.deviceCamera.flip_h = !state.deviceCamera.flip_h;
        btnFlipH.classList.toggle("active", state.deviceCamera.flip_h);
        applyDeviceViewportTransforms();
        saveConfig();
    });

    const btnFlipV = document.getElementById("btnDeviceFlipV");
    btnFlipV?.classList.toggle("active", state.deviceCamera.flip_v);
    btnFlipV?.addEventListener("click", () => {
        state.deviceCamera.flip_v = !state.deviceCamera.flip_v;
        btnFlipV.classList.toggle("active", state.deviceCamera.flip_v);
        applyDeviceViewportTransforms();
        saveConfig();
    });

    // Grid Overlay
    const btnGrid = document.getElementById("btnDeviceToggleGrid");
    const gridEl = document.getElementById("framingGridDevice");
    btnGrid?.classList.toggle("active", state.deviceCamera.grid_active);
    gridEl?.classList.toggle("active", state.deviceCamera.grid_active);
    btnGrid?.addEventListener("click", () => {
        state.deviceCamera.grid_active = !state.deviceCamera.grid_active;
        btnGrid.classList.toggle("active", state.deviceCamera.grid_active);
        gridEl.classList.toggle("active", state.deviceCamera.grid_active);
        saveConfig();
    });

    // Fullscreen
    const btnFullscreen = document.getElementById("btnDeviceFullscreen");
    const deviceCard = document.getElementById("deviceViewportCard");
    btnFullscreen?.addEventListener("click", () => {
        if (!document.fullscreenElement) {
            deviceCard.requestFullscreen().catch(err => console.log(err));
        } else {
            document.exitFullscreen();
        }
    });

    // Focus / Auto Focus Mode & Distance
    const focusSelect = document.getElementById("deviceFocusModeSelect");
    const groupFocusDist = document.getElementById("groupDeviceFocusDistance");
    const focusDistSlider = document.getElementById("deviceFocusDistanceSlider");
    const focusDistBadge = document.getElementById("deviceFocusDistanceBadge");

    if (focusSelect) {
        focusSelect.value = state.deviceCamera.focus_mode || "continuous";
        if (groupFocusDist) {
            groupFocusDist.style.display = focusSelect.value === "manual" ? "flex" : "none";
        }
        focusSelect.addEventListener("change", (e) => {
            state.deviceCamera.focus_mode = e.target.value;
            if (groupFocusDist) {
                groupFocusDist.style.display = e.target.value === "manual" ? "flex" : "none";
            }
            applyDeviceHardwareConstraints();
            saveConfig();
            showToast(`Focus mode: ${e.target.value === "manual" ? "Manual Focus" : "Auto Focus"}`);
        });
    }

    if (focusDistSlider && focusDistBadge) {
        focusDistSlider.value = state.deviceCamera.focus_distance || 0;
        focusDistBadge.textContent = focusDistSlider.value;
        focusDistSlider.addEventListener("input", (e) => {
            state.deviceCamera.focus_distance = parseInt(e.target.value, 10);
            focusDistBadge.textContent = state.deviceCamera.focus_distance;
            applyDeviceHardwareConstraints();
            saveConfig();
        });
    }

    // Settings Inputs
    const sourceSelect = document.getElementById("deviceSourceSelect");
    sourceSelect?.addEventListener("change", async (e) => {
        state.deviceCamera.deviceId = e.target.value;
        const badge = document.getElementById("deviceCamBadge");
        if (badge && sourceSelect.options[sourceSelect.selectedIndex]) {
            badge.textContent = sourceSelect.options[sourceSelect.selectedIndex].text;
        }
        saveConfig();
        if (state.deviceCamera.enabled) {
            await startDeviceCameraStream(true);
        }
    });

    const resSelect = document.getElementById("deviceResSelect");
    if (resSelect && state.deviceCamera.resolution) {
        resSelect.value = state.deviceCamera.resolution;
    }
    resSelect?.addEventListener("change", async (e) => {
        state.deviceCamera.resolution = e.target.value;
        saveConfig();
        if (state.deviceCamera.enabled) {
            await startDeviceCameraStream(true);
        }
    });

    const zoomSlider = document.getElementById("deviceZoomSlider");
    const zoomBadge = document.getElementById("deviceZoomBadge");
    if (zoomSlider && zoomBadge) {
        zoomSlider.value = state.deviceCamera.digital_zoom || 1.0;
        zoomBadge.textContent = `${(state.deviceCamera.digital_zoom || 1.0).toFixed(1)}x`;
        zoomSlider.addEventListener("input", (e) => {
            state.deviceCamera.digital_zoom = parseFloat(e.target.value);
            zoomBadge.textContent = `${state.deviceCamera.digital_zoom.toFixed(1)}x`;
            applyDeviceViewportTransforms();
            saveConfig();
        });
    }

    const brightSlider = document.getElementById("deviceBrightnessSlider");
    const brightBadge = document.getElementById("deviceBrightnessBadge");
    if (brightSlider && brightBadge) {
        brightSlider.value = state.deviceCamera.filters.brightness || 100;
        brightBadge.textContent = `${brightSlider.value}%`;
        brightSlider.addEventListener("input", (e) => {
            state.deviceCamera.filters.brightness = parseInt(e.target.value, 10);
            brightBadge.textContent = `${state.deviceCamera.filters.brightness}%`;
            applyDeviceViewportTransforms();
            saveConfig();
        });
    }

    const contrastSlider = document.getElementById("deviceContrastSlider");
    const contrastBadge = document.getElementById("deviceContrastBadge");
    if (contrastSlider && contrastBadge) {
        contrastSlider.value = state.deviceCamera.filters.contrast || 100;
        contrastBadge.textContent = `${contrastSlider.value}%`;
        contrastSlider.addEventListener("input", (e) => {
            state.deviceCamera.filters.contrast = parseInt(e.target.value, 10);
            contrastBadge.textContent = `${state.deviceCamera.filters.contrast}%`;
            applyDeviceViewportTransforms();
            saveConfig();
        });
    }

    const satSlider = document.getElementById("deviceSaturationSlider");
    const satBadge = document.getElementById("deviceSaturationBadge");
    if (satSlider && satBadge) {
        satSlider.value = state.deviceCamera.filters.saturation ?? 100;
        satBadge.textContent = `${satSlider.value}%`;
        satSlider.addEventListener("input", (e) => {
            state.deviceCamera.filters.saturation = parseInt(e.target.value, 10);
            satBadge.textContent = `${state.deviceCamera.filters.saturation}%`;
            applyDeviceViewportTransforms();
            saveConfig();
        });
    }

    const sharpSlider = document.getElementById("deviceSharpnessSlider");
    const sharpBadge = document.getElementById("deviceSharpnessBadge");
    if (sharpSlider && sharpBadge) {
        sharpSlider.value = state.deviceCamera.filters.sharpness ?? 100;
        sharpBadge.textContent = `${sharpSlider.value}%`;
        sharpSlider.addEventListener("input", (e) => {
            state.deviceCamera.filters.sharpness = parseInt(e.target.value, 10);
            sharpBadge.textContent = `${state.deviceCamera.filters.sharpness}%`;
            applyDeviceViewportTransforms();
            saveConfig();
        });
    }
}

// ==========================================================================
// COMBINED MULTI-CAMERA SNAPSHOT & 50px TWIBBON COMPOSITOR
// ==========================================================================

function formatIndonesianTimestamp(date = new Date()) {
    const months = [
        "Januari", "Februari", "Maret", "April", "Mei", "Juni",
        "Juli", "Agustus", "September", "Oktober", "November", "Desember"
    ];
    const day = String(date.getDate()).padStart(2, "0");
    const month = months[date.getMonth()];
    const year = date.getFullYear();
    const hours = String(date.getHours()).padStart(2, "0");
    const mins = String(date.getMinutes()).padStart(2, "0");
    const secs = String(date.getSeconds()).padStart(2, "0");
    return `${day} ${month} ${year}@${hours}:${mins}:${secs}`;
}

function loadImageFromBlob(blob) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        const url = URL.createObjectURL(blob);
        img.onload = () => {
            URL.revokeObjectURL(url);
            resolve(img);
        };
        img.onerror = (e) => {
            URL.revokeObjectURL(url);
            reject(e);
        };
        img.src = url;
    });
}

function downloadCanvasImage(canvas, filename) {
    canvas.toBlob((blob) => {
        if (!blob) return;
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }, "image/jpeg", 0.95);
}

function drawTwibbonBackground(ctx, totalWidth, totalHeight, border, timestampStr) {
    // 1. Rich dark gradient background matching app aesthetics
    const bgGrad = ctx.createLinearGradient(0, 0, totalWidth, totalHeight);
    bgGrad.addColorStop(0, "#050811");
    bgGrad.addColorStop(0.2, "#0b1528");
    bgGrad.addColorStop(0.5, "#0d2038");
    bgGrad.addColorStop(0.8, "#091427");
    bgGrad.addColorStop(1, "#050811");
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, totalWidth, totalHeight);

    // 2. Inner crisp neon accent line around the content
    ctx.strokeStyle = "rgba(0, 242, 254, 0.45)";
    ctx.lineWidth = 2;
    ctx.strokeRect(border, border, totalWidth - (border * 2), totalHeight - (border * 2));

    // Outer subtle border
    ctx.strokeStyle = "rgba(0, 242, 254, 0.2)";
    ctx.lineWidth = 1;
    ctx.strokeRect(1, 1, totalWidth - 2, totalHeight - 2);

    // 3. Dense scattered tiny watermark: LumenLux with timestamp (e.g. 10 Oktober 2026@13:15:00)
    const scatterText = `✦ LumenLux • ${timestampStr} ✦`;
    ctx.font = "600 8.5px 'JetBrains Mono', 'Outfit', 'Inter', monospace, sans-serif";
    ctx.textBaseline = "middle";

    const textWidth = ctx.measureText(scatterText).width;
    const step = Math.round(textWidth + 24); // Compact gap between items for dense scatter
    const badgeReservedWidth = 230; // Room reserved on top-right for the official badge

    // --- TOP BORDER: Two Staggered Micro-Tracks ---
    for (let x = 12; x < totalWidth - badgeReservedWidth; x += step) {
        ctx.fillStyle = "rgba(0, 242, 254, 0.42)";
        ctx.fillText(scatterText, x, 16);
    }
    for (let x = 12 + Math.round(step * 0.5); x < totalWidth - badgeReservedWidth; x += step) {
        ctx.fillStyle = "rgba(0, 242, 254, 0.28)";
        ctx.fillText(scatterText, x, 34);
    }

    // --- BOTTOM BORDER: Two Staggered Micro-Tracks ---
    for (let x = 12; x < totalWidth - 12; x += step) {
        ctx.fillStyle = "rgba(0, 242, 254, 0.42)";
        ctx.fillText(scatterText, x, totalHeight - 34);
    }
    for (let x = 12 + Math.round(step * 0.5); x < totalWidth - 12; x += step) {
        ctx.fillStyle = "rgba(0, 242, 254, 0.28)";
        ctx.fillText(scatterText, x, totalHeight - 16);
    }

    // --- LEFT BORDER: Two Staggered Vertical Micro-Tracks (rotated 90deg) ---
    // Track 1 (outer)
    ctx.save();
    ctx.translate(16, border + 15);
    ctx.rotate(Math.PI / 2);
    for (let y = 0; y < totalHeight - (border * 2) - 30; y += step) {
        ctx.fillStyle = "rgba(0, 242, 254, 0.42)";
        ctx.fillText(scatterText, y, 0);
    }
    ctx.restore();

    // Track 2 (inner, staggered)
    ctx.save();
    ctx.translate(34, border + 15 + Math.round(step * 0.5));
    ctx.rotate(Math.PI / 2);
    for (let y = 0; y < totalHeight - (border * 2) - 30; y += step) {
        ctx.fillStyle = "rgba(0, 242, 254, 0.28)";
        ctx.fillText(scatterText, y, 0);
    }
    ctx.restore();

    // --- RIGHT BORDER: Two Staggered Vertical Micro-Tracks (rotated 90deg) ---
    // Track 1 (inner)
    ctx.save();
    ctx.translate(totalWidth - 34, border + 15);
    ctx.rotate(Math.PI / 2);
    for (let y = 0; y < totalHeight - (border * 2) - 30; y += step) {
        ctx.fillStyle = "rgba(0, 242, 254, 0.28)";
        ctx.fillText(scatterText, y, 0);
    }
    ctx.restore();

    // Track 2 (outer, staggered)
    ctx.save();
    ctx.translate(totalWidth - 16, border + 15 + Math.round(step * 0.5));
    ctx.rotate(Math.PI / 2);
    for (let y = 0; y < totalHeight - (border * 2) - 30; y += step) {
        ctx.fillStyle = "rgba(0, 242, 254, 0.42)";
        ctx.fillText(scatterText, y, 0);
    }
    ctx.restore();
}

function drawMiddleDivider(ctx, x, y, width) {
    const cx = x + width / 2;
    const pillW = 120;
    const pillH = 34;
    const pillX = cx - pillW / 2;
    const pillY = y - pillH / 2;
    const radius = 8;

    ctx.save();

    // 1. Sleek cyan dashed line extending across entire content width, broken only by center badge
    ctx.strokeStyle = "rgba(0, 242, 254, 0.75)";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([8, 6]);

    // Left dashed segment
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(pillX, y);
    ctx.stroke();

    // Right dashed segment
    ctx.beginPath();
    ctx.moveTo(pillX + pillW, y);
    ctx.lineTo(x + width, y);
    ctx.stroke();

    ctx.setLineDash([]); // Reset dash for badge

    // 2. Futuristic dark glass pill badge in center
    ctx.fillStyle = "rgba(6, 11, 24, 0.92)";
    ctx.strokeStyle = "rgba(0, 242, 254, 0.6)";
    ctx.lineWidth = 1.5;

    ctx.beginPath();
    if (ctx.roundRect) {
        ctx.roundRect(pillX, pillY, pillW, pillH, radius);
    } else {
        ctx.moveTo(pillX + radius, pillY);
        ctx.lineTo(pillX + pillW - radius, pillY);
        ctx.quadraticCurveTo(pillX + pillW, pillY, pillX + pillW, pillY + radius);
        ctx.lineTo(pillX + pillW, pillY + pillH - radius);
        ctx.quadraticCurveTo(pillX + pillW, pillY + pillH, pillX + pillW - radius, pillY + pillH);
        ctx.lineTo(pillX + radius, pillY + pillH);
        ctx.quadraticCurveTo(pillX, pillY + pillH, pillX, pillY + pillH - radius);
        ctx.lineTo(pillX, pillY + radius);
        ctx.quadraticCurveTo(pillX, pillY, pillX + radius, pillY);
    }
    ctx.fill();
    ctx.stroke();

    // 3. Chamber and Device Labels + Divider Symbol
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    // Top: Chamber (Cyan)
    ctx.font = "700 11.5px 'Outfit', 'Inter', sans-serif";
    ctx.fillStyle = "#38bdf8";
    ctx.fillText("Chamber", cx, y - 7);

    // Center divider symbol: ↕
    ctx.font = "700 9.5px monospace";
    ctx.fillStyle = "rgba(0, 242, 254, 0.7)";
    ctx.fillText("↕", cx, y);

    // Bottom: Device (Purple)
    ctx.font = "700 11.5px 'Outfit', 'Inter', sans-serif";
    ctx.fillStyle = "#c084fc";
    ctx.fillText("Device", cx, y + 8);

    ctx.restore();
}

function drawLumenLuxTwibbonStamp(ctx, totalWidth, totalHeight, border) {
    const badgeW = 200;
    const badgeH = 38;
    const badgeX = totalWidth - badgeW - 14;
    const badgeY = Math.round((border - badgeH) / 2); // Positioned inside top-right border

    ctx.save();

    // 1. Sleek Glass Badge Base
    ctx.fillStyle = "rgba(9, 16, 32, 0.94)";
    ctx.strokeStyle = "rgba(0, 242, 254, 0.65)";
    ctx.lineWidth = 1.5;

    // Rounded rectangle
    const radius = 8;
    ctx.beginPath();
    ctx.moveTo(badgeX + radius, badgeY);
    ctx.lineTo(badgeX + badgeW - radius, badgeY);
    ctx.quadraticCurveTo(badgeX + badgeW, badgeY, badgeX + badgeW, badgeY + radius);
    ctx.lineTo(badgeX + badgeW, badgeY + badgeH - radius);
    ctx.quadraticCurveTo(badgeX + badgeW, badgeY + badgeH, badgeX + badgeW - radius, badgeY + badgeH);
    ctx.lineTo(badgeX + radius, badgeY + badgeH);
    ctx.quadraticCurveTo(badgeX, badgeY + badgeH, badgeX, badgeY + badgeH - radius);
    ctx.lineTo(badgeX, badgeY + radius);
    ctx.quadraticCurveTo(badgeX, badgeY, badgeX + radius, badgeY);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    // 2. Glowing Stamp Icon (Aperture / Camera Symbol)
    const iconX = badgeX + 22;
    const iconY = badgeY + badgeH / 2;

    ctx.strokeStyle = "#00f2fe";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(iconX, iconY, 11, 0, Math.PI * 2);
    ctx.stroke();

    ctx.fillStyle = "#00f2fe";
    ctx.beginPath();
    ctx.arc(iconX, iconY, 4, 0, Math.PI * 2);
    ctx.fill();

    // 3. LumenLux Brand Typography
    ctx.fillStyle = "#ffffff";
    ctx.font = "800 13px 'Outfit', sans-serif";
    ctx.textBaseline = "alphabetic";
    ctx.fillText("LumenLux", badgeX + 44, badgeY + 18);

    ctx.fillStyle = "#f59e0b";
    ctx.font = "700 8px 'Inter', sans-serif";
    ctx.letterSpacing = "1px";
    ctx.fillText("Calibration Capture", badgeX + 44, badgeY + 30);

    // 4. Certified Check Stamp Badge on the right
    ctx.fillStyle = "rgba(16, 185, 129, 0.2)";
    ctx.strokeStyle = "rgba(16, 185, 129, 0.8)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(badgeX + badgeW - 20, iconY, 10, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = "#10b981";
    ctx.font = "bold 9px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("✓", badgeX + badgeW - 20, iconY);

    ctx.restore();
}

async function captureMultiCameraSnapshot() {
    const isChamberActive = !!state.camera.enabled;
    const isDeviceActive = !!(state.deviceCamera && state.deviceCamera.active && state.deviceCamera.stream);

    if (!isChamberActive && !isDeviceActive) {
        showToast("No active cameras to capture snapshot from");
        return;
    }

    showToast("Compositing high-resolution multi-camera snapshot...");

    try {
        let imgChamber = null;
        let imgDevice = null;

        // 1. Grab Chamber image
        if (isChamberActive) {
            try {
                const res = await fetch("/api/camera/snapshot");
                if (res.ok) {
                    const blob = await res.blob();
                    imgChamber = await loadImageFromBlob(blob);
                }
            } catch (e) {
                console.warn("API snapshot fetch failed, capturing from live stream element:", e);
            }

            if (!imgChamber) {
                const streamImg = document.getElementById("cameraStream");
                if (streamImg && streamImg.naturalWidth) {
                    imgChamber = streamImg;
                }
            }
        }

        // 2. Grab Device frame
        if (isDeviceActive) {
            const video = document.getElementById("deviceCameraVideo");
            if (video && video.videoWidth > 0) {
                const offCanvas = document.createElement("canvas");
                offCanvas.width = video.videoWidth;
                offCanvas.height = video.videoHeight;
                const offCtx = offCanvas.getContext("2d");
                offCtx.drawImage(video, 0, 0, offCanvas.width, offCanvas.height);
                imgDevice = offCanvas;
            }
        }

        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        const border = 50; // 50px outside border on all sides
        const timestampStr = formatIndonesianTimestamp();
        const cleanTimestamp = timestampStr.replace(/[@:]/g, "-").replace(/\s+/g, "_");

        const slotW = 1920;
        const slotH = 1080;

        if (imgChamber && imgDevice) {
            // === DUAL MULTI-CAMERA VERTICAL STACK (2020 x 2260) ===
            canvas.width = slotW + (border * 2); // 1920 + 100 = 2020
            canvas.height = (slotH * 2) + (border * 2); // 2160 + 100 = 2260

            // Draw Twibbon Gradient Background & Scattered Timestamp Watermark
            drawTwibbonBackground(ctx, canvas.width, canvas.height, border, timestampStr);

            // Draw Chamber Image (Upper Slot: x=50, y=50, w=1920, h=1080)
            ctx.save();
            ctx.beginPath();
            ctx.rect(border, border, slotW, slotH);
            ctx.clip();
            const cx = border + slotW / 2;
            const cy = border + slotH / 2;
            ctx.translate(cx, cy);
            ctx.scale(state.camera.flip_h ? -1 : 1, state.camera.flip_v ? -1 : 1);
            const z1 = state.camera.digital_zoom || 1.0;
            ctx.scale(z1, z1);
            ctx.filter = `brightness(${state.camera.filters.brightness}%) contrast(${state.camera.filters.contrast}%) saturate(${state.camera.filters.saturation}%)`;
            const w1 = imgChamber.naturalWidth || imgChamber.width;
            const h1 = imgChamber.naturalHeight || imgChamber.height;
            const coverScale1 = Math.max(slotW / w1, slotH / h1);
            const drawW1 = w1 * coverScale1;
            const drawH1 = h1 * coverScale1;
            ctx.drawImage(imgChamber, -drawW1 / 2, -drawH1 / 2, drawW1, drawH1);
            ctx.restore();

            // Draw Device Image (Bottom Slot: x=50, y=1130, w=1920, h=1080)
            const divY = border + slotH; // 50 + 1080 = 1130
            ctx.save();
            ctx.beginPath();
            ctx.rect(border, divY, slotW, slotH);
            ctx.clip();
            const dx = border + slotW / 2;
            const dy = divY + slotH / 2;
            ctx.translate(dx, dy);

            // Apply Device Camera Rotation (0°, 90°, 180°, 270°)
            const rot = state.deviceCamera.rotation || 0;
            ctx.rotate((rot * Math.PI) / 180);

            // Apply Flip and Zoom
            const z2 = state.deviceCamera.digital_zoom || 1.0;
            ctx.scale((state.deviceCamera.flip_h ? -1 : 1) * z2, (state.deviceCamera.flip_v ? -1 : 1) * z2);

            // Apply Device Filters (brightness, contrast, saturation, sharpness)
            const devFilters = state.deviceCamera.filters || {};
            const devBright = devFilters.brightness ?? 100;
            const devContrast = devFilters.contrast ?? 100;
            const devSat = devFilters.saturation ?? 100;
            const devSharp = devFilters.sharpness ?? 100;
            let filterStr = `brightness(${devBright}%) contrast(${devContrast}%) saturate(${devSat}%)`;
            if (devSharp !== 100) {
                const sharpFactor = (devSharp - 100) / 100;
                filterStr += ` contrast(${100 + sharpFactor * 25}%)`;
            }
            ctx.filter = filterStr;

            // Aspect fit/cover calculation matching live browser preview exactly
            const w2 = imgDevice.width;
            const h2 = imgDevice.height;
            const fitMode = state.deviceCamera.fit_mode || "contain";
            let scale2;
            if (rot === 90 || rot === 270) {
                scale2 = fitMode === "cover"
                    ? Math.max(slotH / w2, slotW / h2)
                    : Math.min(slotH / w2, slotW / h2);
            } else {
                scale2 = fitMode === "cover"
                    ? Math.max(slotW / w2, slotH / h2)
                    : Math.min(slotW / w2, slotH / h2);
            }
            const drawW2 = w2 * scale2;
            const drawH2 = h2 * scale2;
            ctx.drawImage(imgDevice, -drawW2 / 2, -drawH2 / 2, drawW2, drawH2);
            ctx.restore();

            // Draw Middle Divider (Thin dashed seam across full width with Chamber/Device indicator)
            drawMiddleDivider(ctx, border, divY, slotW);

            // Draw Official LumenLux Stamp on right corner of twibbon
            drawLumenLuxTwibbonStamp(ctx, canvas.width, canvas.height, border);

            // Download Combined Picture
            downloadCanvasImage(canvas, `LumenLux_MultiCam_${cleanTimestamp}.jpg`);
            showToast(`Combined dual-snapshot downloaded (${canvas.width}x${canvas.height})!`);

        } else if (imgChamber) {
            // === SINGLE CHAMBER CAMERA WITH 50px TWIBBON (2020 x 1180) ===
            canvas.width = slotW + (border * 2);
            canvas.height = slotH + (border * 2);

            drawTwibbonBackground(ctx, canvas.width, canvas.height, border, timestampStr);

            ctx.save();
            ctx.beginPath();
            ctx.rect(border, border, slotW, slotH);
            ctx.clip();
            const cx = border + slotW / 2;
            const cy = border + slotH / 2;
            ctx.translate(cx, cy);
            ctx.scale(state.camera.flip_h ? -1 : 1, state.camera.flip_v ? -1 : 1);
            const z = state.camera.digital_zoom || 1.0;
            ctx.scale(z, z);
            ctx.filter = `brightness(${state.camera.filters.brightness}%) contrast(${state.camera.filters.contrast}%) saturate(${state.camera.filters.saturation}%)`;
            const w1 = imgChamber.naturalWidth || imgChamber.width;
            const h1 = imgChamber.naturalHeight || imgChamber.height;
            const coverScale1 = Math.max(slotW / w1, slotH / h1);
            const drawW1 = w1 * coverScale1;
            const drawH1 = h1 * coverScale1;
            ctx.drawImage(imgChamber, -drawW1 / 2, -drawH1 / 2, drawW1, drawH1);
            ctx.restore();

            drawLumenLuxTwibbonStamp(ctx, canvas.width, canvas.height, border);
            downloadCanvasImage(canvas, `LumenLux_Chamber_${cleanTimestamp}.jpg`);
            showToast(`Chamber snapshot downloaded (${canvas.width}x${canvas.height})!`);

        } else if (imgDevice) {
            // === SINGLE DEVICE CAMERA WITH 50px TWIBBON (2020 x 1180) ===
            canvas.width = slotW + (border * 2);
            canvas.height = slotH + (border * 2);

            drawTwibbonBackground(ctx, canvas.width, canvas.height, border, timestampStr);

            ctx.save();
            ctx.beginPath();
            ctx.rect(border, border, slotW, slotH);
            ctx.clip();
            const dx = border + slotW / 2;
            const dy = border + slotH / 2;
            ctx.translate(dx, dy);

            const rot = state.deviceCamera.rotation || 0;
            ctx.rotate((rot * Math.PI) / 180);

            const z2 = state.deviceCamera.digital_zoom || 1.0;
            ctx.scale((state.deviceCamera.flip_h ? -1 : 1) * z2, (state.deviceCamera.flip_v ? -1 : 1) * z2);

            const devFilters = state.deviceCamera.filters || {};
            const devBright = devFilters.brightness ?? 100;
            const devContrast = devFilters.contrast ?? 100;
            const devSat = devFilters.saturation ?? 100;
            const devSharp = devFilters.sharpness ?? 100;
            let filterStr = `brightness(${devBright}%) contrast(${devContrast}%) saturate(${devSat}%)`;
            if (devSharp !== 100) {
                const sharpFactor = (devSharp - 100) / 100;
                filterStr += ` contrast(${100 + sharpFactor * 25}%)`;
            }
            ctx.filter = filterStr;

            const w2 = imgDevice.width;
            const h2 = imgDevice.height;
            const fitMode = state.deviceCamera.fit_mode || "contain";
            let scale2;
            if (rot === 90 || rot === 270) {
                scale2 = fitMode === "cover"
                    ? Math.max(slotH / w2, slotW / h2)
                    : Math.min(slotH / w2, slotW / h2);
            } else {
                scale2 = fitMode === "cover"
                    ? Math.max(slotW / w2, slotH / h2)
                    : Math.min(slotW / w2, slotH / h2);
            }
            const drawW2 = w2 * scale2;
            const drawH2 = h2 * scale2;
            ctx.drawImage(imgDevice, -drawW2 / 2, -drawH2 / 2, drawW2, drawH2);
            ctx.restore();

            drawLumenLuxTwibbonStamp(ctx, canvas.width, canvas.height, border);
            downloadCanvasImage(canvas, `LumenLux_Device_${cleanTimestamp}.jpg`);
            showToast(`Device snapshot downloaded (${canvas.width}x${canvas.height})!`);
        }
    } catch (err) {
        console.error("Multi-camera snapshot failed:", err);
        showToast("Snapshot error: " + err.message);
    }
}

// ==========================================================================
// ASSISTIVE TOUCH FLOATING MASTER ACTION BAR (MOBILE / SMARTPHONE VIEW)
// ==========================================================================
const FLOATING_POS_KEY = "lumen_floating_actions_pos_v2";

function setupFloatingAssistiveTouch() {
    const toolbar = document.getElementById("unifiedActionToolbar");
    if (!toolbar) return;

    function isMobileMode() {
        return window.innerWidth <= 1024 || window.matchMedia("(pointer: coarse)").matches;
    }

    function getViewportBounds() {
        const vv = window.visualViewport;
        const vw = vv ? vv.width : window.innerWidth;
        const vh = vv ? vv.height : window.innerHeight;
        return { vw, vh };
    }

    function restorePosition() {
        if (!isMobileMode()) {
            toolbar.style.left = "";
            toolbar.style.top = "";
            toolbar.style.right = "";
            toolbar.style.bottom = "";
            return;
        }

        try {
            const saved = localStorage.getItem(FLOATING_POS_KEY);
            const rect = toolbar.getBoundingClientRect();
            const w = rect.width || 180;
            const h = rect.height || 96;
            const { vw, vh } = getViewportBounds();
            const minLeft = 8;
            const minTop = 56; // Header height safety margin (never under 50px header)
            const maxLeft = Math.max(minLeft, vw - w - 8);
            const maxTop = Math.max(minTop, vh - h - 8);

            if (saved) {
                const { leftPct, topPct } = JSON.parse(saved);
                let targetLeft = (leftPct / 100) * vw;
                let targetTop = (topPct / 100) * vh;
                targetLeft = Math.max(minLeft, Math.min(targetLeft, maxLeft));
                targetTop = Math.max(minTop, Math.min(targetTop, maxTop));

                toolbar.style.left = `${Math.round(targetLeft)}px`;
                toolbar.style.top = `${Math.round(targetTop)}px`;
                toolbar.style.right = "auto";
                toolbar.style.bottom = "auto";
            } else {
                // Default position: bottom-right
                const defLeft = Math.max(minLeft, vw - w - 16);
                const defTop = Math.max(minTop, vh - h - 24);
                toolbar.style.left = `${Math.round(defLeft)}px`;
                toolbar.style.top = `${Math.round(defTop)}px`;
                toolbar.style.right = "auto";
                toolbar.style.bottom = "auto";
            }
        } catch (e) {
            console.warn("[AssistiveTouch] Position restore error:", e);
        }
    }

    let isDragging = false;
    let startX = 0;
    let startY = 0;
    let initialLeft = 0;
    let initialTop = 0;
    let hasMoved = false;
    const DRAG_THRESHOLD = 5;

    function handleStart(e) {
        if (!isMobileMode()) return;
        const pointer = e.touches ? e.touches[0] : e;
        isDragging = true;
        hasMoved = false;
        startX = pointer.clientX;
        startY = pointer.clientY;

        const rect = toolbar.getBoundingClientRect();
        initialLeft = rect.left;
        initialTop = rect.top;
        toolbar.classList.add("is-dragging");

        // Prevent native gesture scrolling when touching drag handle
        if (e.cancelable && e.type === "touchstart") {
            const isButton = e.target.closest("button");
            if (!isButton || e.target.closest(".floating-drag-handle")) {
                e.preventDefault();
            }
        }
    }

    function handleMove(e) {
        if (!isDragging) return;
        const pointer = e.touches ? e.touches[0] : e;
        const deltaX = pointer.clientX - startX;
        const deltaY = pointer.clientY - startY;

        if (Math.hypot(deltaX, deltaY) > DRAG_THRESHOLD) {
            hasMoved = true;
        }

        if (hasMoved) {
            if (e.cancelable) e.preventDefault(); // Never scroll or rubberband the document while dragging!
            const rect = toolbar.getBoundingClientRect();
            const { vw, vh } = getViewportBounds();
            const minLeft = 8;
            const minTop = 56;
            const maxLeft = Math.max(minLeft, vw - rect.width - 8);
            const maxTop = Math.max(minTop, vh - rect.height - 8);

            const newLeft = Math.max(minLeft, Math.min(initialLeft + deltaX, maxLeft));
            const newTop = Math.max(minTop, Math.min(initialTop + deltaY, maxTop));

            toolbar.style.left = `${Math.round(newLeft)}px`;
            toolbar.style.top = `${Math.round(newTop)}px`;
            toolbar.style.right = "auto";
            toolbar.style.bottom = "auto";
        }
    }

    function handleEnd() {
        if (!isDragging) return;
        isDragging = false;
        toolbar.classList.remove("is-dragging");

        if (hasMoved) {
            // Save relative percentage position to localStorage
            const rect = toolbar.getBoundingClientRect();
            const { vw, vh } = getViewportBounds();
            const leftPct = (rect.left / vw) * 100;
            const topPct = (rect.top / vh) * 100;
            try {
                localStorage.setItem(FLOATING_POS_KEY, JSON.stringify({ leftPct, topPct }));
            } catch (err) { }

            // Suppress the click event triggered right after dragging
            const suppressClick = (clickEvent) => {
                clickEvent.stopPropagation();
                clickEvent.preventDefault();
                window.removeEventListener("click", suppressClick, true);
            };
            window.addEventListener("click", suppressClick, true);
            setTimeout(() => {
                window.removeEventListener("click", suppressClick, true);
            }, 120);
        }
    }

    // Touch Event Listeners (Mobile / Smartphone)
    toolbar.addEventListener("touchstart", handleStart, { passive: false });
    window.addEventListener("touchmove", handleMove, { passive: false });
    window.addEventListener("touchend", handleEnd);
    window.addEventListener("touchcancel", handleEnd);

    // Mouse Event Listeners (Desktop mobile responsive simulation)
    toolbar.addEventListener("mousedown", handleStart);
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleEnd);

    // Reposition on screen resize or orientation change
    window.addEventListener("resize", debounce(restorePosition, 100));

    // Initial position restore
    setTimeout(restorePosition, 80);
}

// ==========================================================================
// CIRCULAR ARROW SETTINGS RESET TO DEFAULT (↺)
// ==========================================================================

function setupSettingsResetButtons() {
    const resetButtons = document.querySelectorAll(".btn-setting-reset");
    if (!resetButtons.length) return;

    resetButtons.forEach(btn => {
        const targetId = btn.getAttribute("data-target");
        const targetNumId = btn.getAttribute("data-target-num");
        const autoId = btn.getAttribute("data-auto");
        const badgeId = btn.getAttribute("data-badge");
        const badgeSuffix = btn.getAttribute("data-badge-suffix") || "";
        const defaultVal = btn.getAttribute("data-default");
        if (!targetId || defaultVal === null) return;

        const targetEl = document.getElementById(targetId);
        const targetNumEl = targetNumId ? document.getElementById(targetNumId) : null;
        if (!targetEl) return;

        function checkVisibility() {
            const currentVal = targetEl.value;
            const isDiff = String(currentVal) !== String(defaultVal);
            btn.classList.toggle("visible", isDiff);
        }

        // Listen for user changes
        targetEl.addEventListener("input", checkVisibility);
        targetEl.addEventListener("change", checkVisibility);
        if (targetNumEl) {
            targetNumEl.addEventListener("input", checkVisibility);
            targetNumEl.addEventListener("change", checkVisibility);
        }

        // Click handler to restore to default setting
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            e.preventDefault();

            targetEl.value = defaultVal;
            if (targetNumEl) {
                targetNumEl.value = defaultVal;
            }
            if (badgeId) {
                const badgeEl = document.getElementById(badgeId);
                if (badgeEl) {
                    badgeEl.textContent = `${defaultVal}${badgeSuffix}`;
                }
            }
            if (autoId) {
                const autoEl = document.getElementById(autoId);
                if (autoEl) {
                    autoEl.checked = true;
                    targetEl.disabled = true;
                    if (targetNumEl) targetNumEl.disabled = true;
                }
            }

            // Dispatch input and change events so camera listeners and API calls trigger
            targetEl.dispatchEvent(new Event("input", { bubbles: true }));
            targetEl.dispatchEvent(new Event("change", { bubbles: true }));

            btn.classList.remove("visible");

            const label = btn.closest(".props-row")?.querySelector(".props-label")?.textContent?.trim()
                || btn.closest(".field-group")?.querySelector(".field-label")?.textContent?.trim()
                || "Setting";
            showToast(`${label} restored to default (${defaultVal})`);
        });

        // Initial check
        checkVisibility();
    });

    // Expose global updater so when backend controls or presets sync, reset arrows update
    window.updateResetButtonsState = () => {
        resetButtons.forEach(btn => {
            const targetId = btn.getAttribute("data-target");
            const defaultVal = btn.getAttribute("data-default");
            const targetEl = document.getElementById(targetId);
            if (targetEl && defaultVal !== null) {
                btn.classList.toggle("visible", String(targetEl.value) !== String(defaultVal));
            }
        });
    };
}

// --- INITIALIZATION ---

document.addEventListener("DOMContentLoaded", () => {
    console.log("Initializing LumenLux-Zero Dashboard...");

    // 1. Load saved preferences from browser LocalStorage
    loadSavedConfig();

    // 2. Setup Virtual LED strip and visualizer loop
    initVirtualLedStrip();
    startVisualizerLoop();

    // 3. Bind UI listeners
    setupLightListeners();
    setupCameraListeners();
    setupDeviceCameraManager();
    setupOcrAuth();
    setupResetToDefaults();
    setupMobileLedDrawer();
    setupFloatingAssistiveTouch();
    setupSettingsResetButtons();
    initFpsCounter();
    applyViewportTransforms();
    updateSnapshotButtonState();

    // 4. Inactivity & Heartbeat tracking
    setupInactivityHeartbeat();

    // 5. Initial Camera & Device discovery (use pre-rendered camera list if available)
    if (window.INITIAL_CAMERAS && window.INITIAL_CAMERAS.length > 0) {
        applyCameraList(window.INITIAL_CAMERAS);
    }
    refreshCameraList();

    // 6. Initial status query and start low-frequency heartbeat (every 6 seconds)
    pollSystemStatus();
    setInterval(pollSystemStatus, 6000);

    // Initial sync of RGB UI (do not send to backend on boot)
    syncRgbInputs(state.lights.color.r, state.lights.color.g, state.lights.color.b, false, false);
});

