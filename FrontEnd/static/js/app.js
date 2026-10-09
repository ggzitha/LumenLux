/**
 * Lumen-Zero High-Performance Frontend Engine.
 * Handles client-side heavy lifting, reactive controls, video streaming,
 * FPS calculation, LED strip simulation, and browser LocalStorage persistence.
 */

// Global State
const state = {
    // Light state (Defaults: Power ON, White, 30% brightness, Static effect)
    lights: {
        power: true,
        color: { r: 255, g: 255, b: 255, hex: "#ffffff" },
        brightness: 1.0,
        brightness_pct: 100,
        effect: "static",
        speed: 50
    },
    // Camera state
    camera: {
        enabled: true,
        active_id: null,
        resolution: "640x480",
        target_fps: 15,
        digital_zoom: 1.0,
        flip_h: false,
        flip_v: false,
        grid_active: false,
        filters: {
            brightness: 100,
            contrast: 100,
            saturation: 100
        },
        controls: {}
    },
    system: {
        online: false,
        latency_ms: 0
    }
};

// --- LOCAL STORAGE CACHE HELPERS ---
const STORAGE_KEY = "lumen_zero_user_config_v1";

function loadSavedConfig() {
    try {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (saved) {
            const parsed = JSON.parse(saved);
            if (parsed.lights) Object.assign(state.lights, parsed.lights);
            if (parsed.camera) Object.assign(state.camera, parsed.camera);
            console.log("[Storage] User configuration loaded from browser storage:", parsed);
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
    const NODE_COUNT = 36; // Visual simulator nodes
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
        this.onChange = options.onChange || (() => {});

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

function syncRgbInputs(r, g, b, fromWheel = false) {
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

    sendLightUpdate(false);
}

function setMasterPower(on) {
    state.lights.power = !!on;
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

    // Presets (White, Warm White, Red, Green, Blue)
    document.getElementById("presetWhite")?.addEventListener("click", () => syncRgbInputs(255, 255, 255, false));
    document.getElementById("presetWarmWhite")?.addEventListener("click", () => syncRgbInputs(255, 214, 164, false));
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

async function refreshCameraList() {
    try {
        const res = await fetch("/api/cameras");
        if (!res.ok) return;
        const data = await res.json();
        const select = document.getElementById("cameraSelect");
        select.innerHTML = "";

        if (data.cameras && data.cameras.length > 0) {
            data.cameras.forEach(cam => {
                const opt = document.createElement("option");
                opt.value = cam.id;
                opt.textContent = `${cam.name} (${cam.type.toUpperCase()})`;
                if (cam.id === state.camera.active_id || cam.active) {
                    opt.selected = true;
                    state.camera.active_id = cam.id;
                }
                select.appendChild(opt);
            });
            document.getElementById("activeCamBadge").textContent = select.options[select.selectedIndex]?.text || "Camera Connected";
        } else {
            const opt = document.createElement("option");
            opt.value = "none";
            opt.textContent = "No camera detected";
            select.appendChild(opt);
        }
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

        const btnSnapshot = document.getElementById("btnSnapshot");
        if (btnSnapshot) {
            btnSnapshot.disabled = !state.camera.enabled;
            btnSnapshot.classList.toggle("disabled", !state.camera.enabled);
            btnSnapshot.title = state.camera.enabled 
                ? "Capture high-resolution snapshot" 
                : "Camera is disabled (enable camera to capture snapshot)";
        }
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

    // Resolution & FPS
    const resSelect = document.getElementById("resolutionSelect");
    resSelect.value = state.camera.resolution;
    resSelect.addEventListener("change", (e) => {
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

    // Snapshot Download (Guarded against disabled camera)
    const btnSnapshot = document.getElementById("btnSnapshot");
    btnSnapshot?.addEventListener("click", async () => {
        if (!state.camera.enabled) {
            showToast("Camera is disabled — enable camera to capture snapshot");
            return;
        }
        try {
            showToast("Capturing high-resolution snapshot...");
            const res = await fetch("/api/camera/snapshot");
            if (!res.ok) throw new Error("Snapshot error");
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
            a.download = `LumenLux_snapshot_${timestamp}.jpg`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            showToast("Snapshot downloaded successfully!");
        } catch (e) {
            showToast("Snapshot error: " + e.message);
        }
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

    // PowerLine Frequency Dropdown
    const powerlineSelect = document.getElementById("vpaPowerlineFreq");
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

function setupInactivityHeartbeat() {
    const events = ["mousemove", "keydown", "click", "touchstart", "scroll"];
    events.forEach(evt => {
        window.addEventListener(evt, () => {
            lastUserInteraction = Date.now();
            userActiveSinceLastHeartbeat = true;
        }, { passive: true });
    });

    const timerEl = document.getElementById("statIdleTimer");
    const timerDrawerEl = document.getElementById("statIdleTimerDrawer");

    const updateTimerText = (formattedTime, isWarning) => {
        if (timerEl) {
            timerEl.textContent = formattedTime;
            timerEl.style.color = isWarning ? "var(--neon-red)" : "";
        }
        if (timerDrawerEl) {
            timerDrawerEl.textContent = formattedTime;
            timerDrawerEl.style.color = isWarning ? "var(--neon-red)" : "";
        }
    };

    // Periodic heartbeat every 20 seconds while user is active
    setInterval(async () => {
        if (userActiveSinceLastHeartbeat) {
            try {
                const res = await fetch("/api/heartbeat", { method: "POST" });
                if (res.ok) {
                    const data = await res.json();
                    userActiveSinceLastHeartbeat = false;
                    if (data.remaining_seconds !== undefined) {
                        const m = Math.floor(data.remaining_seconds / 60);
                        const s = data.remaining_seconds % 60;
                        const formatted = `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
                        updateTimerText(formatted, data.remaining_seconds <= 60);
                    }
                }
            } catch (e) {
                // Ignore transient network errors
            }
        }
    }, 20000);

    // Update countdown timer display every 1 second
    setInterval(() => {
        const elapsedSec = Math.floor((Date.now() - lastUserInteraction) / 1000);
        const remaining = Math.max(0, 600 - elapsedSec);
        const m = Math.floor(remaining / 60);
        const s = remaining % 60;
        const formatted = `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
        updateTimerText(formatted, remaining <= 60);

        if (remaining === 0 && (state.lights.power || state.camera.enabled)) {
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
            showToast("Inactivity Timeout: Automatically powered off camera and lights.");
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
    setupMobileLedDrawer();
    initFpsCounter();
    applyViewportTransforms();

    // 4. Inactivity & Heartbeat tracking
    setupInactivityHeartbeat();

    // 5. Initial Camera & Device discovery
    refreshCameraList();

    // 6. Initial status query and start low-frequency heartbeat (every 6 seconds)
    pollSystemStatus();
    setInterval(pollSystemStatus, 6000);

    // Initial sync of RGB
    syncRgbInputs(state.lights.color.r, state.lights.color.g, state.lights.color.b);
});
