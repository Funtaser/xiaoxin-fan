// SPDX-License-Identifier: GPL-3.0-or-later
// 小新风扇控制：快捷设置面板，前端连接系统总线上的 xiaoxin-fand。
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';

const BUS_NAME = 'io.github.XiaoxinFan';
const OBJECT_PATH = '/io/github/XiaoxinFan';
const IFACE_XML = `
<node>
  <interface name="io.github.XiaoxinFan1">
    <method name="GetStatus"><arg type="s" direction="out"/></method>
    <method name="ListPresets"><arg type="s" direction="out"/></method>
    <method name="SetPreset"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <signal name="StatusChanged"><arg type="s"/></signal>
  </interface>
</node>`;
const FanProxy = Gio.DBusProxy.makeProxyWrapper(IFACE_XML);

const PRESET_ICONS = {
    stock: 'emblem-system-symbolic',
    battery: 'battery-good-symbolic',
    quiet: 'weather-clear-night-symbolic',
    balanced: 'power-profile-balanced-symbolic',
    performance: 'power-profile-performance-symbolic',
    geek: 'icons/xiaoxin-geek-symbolic.svg',     // 闪电（随扩展附带，不依赖图标主题）
};
const DEFAULT_ON_PRESET = 'balanced';    // 从「原厂」点亮开关时切到的预设（首次）
const FAN_ICON = 'icons/xiaoxin-fan-symbolic.svg';
const HISTORY = 120;                     // 小曲线保留的样本数（守护进程每秒推送一次 → 2 分钟）
const PL1_W = {'low-power': 20, 'balanced': 50, 'performance': 70};   // EC 各档位的持续功耗上限（实测）
const SENSOR_NAMES = {cpu: 'CPU', sen1: '主板', sen2: '主板', nvme0: '硬盘', nvme1: '硬盘'};
const MUTED = 170;                       // 次要文字的不透明度（0–255）

// 'icons/...' 为扩展自带图标，其余为系统图标名
function presetIcon(path, id) {
    const icon = PRESET_ICONS[id] ?? 'emblem-system-symbolic';
    return icon.startsWith('icons/') ? Gio.icon_new_for_string(`${path}/${icon}`) : icon;
}

// 预设列表右侧的要点：持续功耗上限 · 最高转速
function presetHint(p) {
    if (!p.curve)
        return 'EC 控制';
    const watts = p.extras?.includes('stp') ? 10 : PL1_W[p.profile];
    const maxRpm = Math.max(...p.curve.map(([, rpm]) => rpm));
    return `${watts ? `${watts} W · ` : ''}≤${maxRpm}`;
}

// 风扇读数：两个风扇接近时只显示一个数
function fanReading(s) {
    if (!s?.fans)
        return {value: '—', unit: ''};
    const [a, b] = s.fans;
    if (a === 0 && b === 0)
        return {value: '停转', unit: ''};
    return {value: Math.abs(a - b) <= 100 ? `${Math.max(a, b)}` : `${a}/${b}`, unit: 'rpm'};
}

const num = v => typeof v === 'number' && Number.isFinite(v);

// 底部状态区要显示的提示（无需提示时返回 null）
function statusNote(s) {
    if (s.error)
        return {warn: true, icon: 'dialog-warning-symbolic', text: `出错，风扇已交还 EC：${s.error}`};
    if (s.power_cap)
        return {warn: true, icon: 'dialog-warning-symbolic', text: `主板温度过高，已限制功耗至 ${s.power_cap} W`};
    if (s.reason?.startsWith('guard:'))
        return {warn: true, icon: 'dialog-warning-symbolic',
            text: `${SENSOR_NAMES[s.reason.slice(6)] ?? s.reason.slice(6)}温度过高，风扇已提速`};
    if (s.reason === 'sensor-fault')
        return {warn: true, icon: 'dialog-warning-symbolic', text: '温度读取异常，风扇已交还 EC'};
    if (s.reason === 'ec-idle')
        return {warn: false, icon: null, text: '温度较低，风扇交给 EC（可停转）'};
    return null;
}

function makeTile(caption) {
    const box = new St.BoxLayout({style_class: 'xiaoxin-tile', orientation: Clutter.Orientation.VERTICAL, x_expand: true});
    const cap = new St.Label({text: caption, style_class: 'xiaoxin-tile-caption', opacity: MUTED});
    const row = new St.BoxLayout();
    const value = new St.Label({text: '—', style_class: 'xiaoxin-tile-value', y_align: Clutter.ActorAlign.END});
    const unit = new St.Label({text: '', style_class: 'xiaoxin-tile-unit', y_align: Clutter.ActorAlign.END});
    row.add_child(value);
    row.add_child(unit);
    box.add_child(cap);
    box.add_child(row);
    return {box, value, unit};
}

// 温度、风扇、功耗三格读数 + 近 2 分钟小曲线 + 状态提示
const StatusItem = GObject.registerClass(
class StatusItem extends PopupMenu.PopupBaseMenuItem {
    _init(fanGicon) {
        super._init({reactive: false, can_focus: false});
        // 不可点击的菜单项会被 GNOME 整体调暗；这里是读数区，保持正常亮度（次要文字已单独设置透明度）
        this.remove_style_class_name('popup-inactive-menu-item');
        this._temps = [];
        this._fans = [];

        const col = new St.BoxLayout({style_class: 'xiaoxin-status', orientation: Clutter.Orientation.VERTICAL, x_expand: true});
        const tiles = new St.BoxLayout({style_class: 'xiaoxin-tiles', x_expand: true});
        this._tTemp = makeTile('CPU / 主板');
        this._tFan = makeTile('风扇');
        this._tPower = makeTile('功耗');
        for (const t of [this._tTemp, this._tFan, this._tPower])
            tiles.add_child(t.box);
        col.add_child(tiles);

        this._chart = new St.DrawingArea({style_class: 'xiaoxin-chart', x_expand: true});
        this._chart.connect('repaint', area => this._paint(area));
        col.add_child(this._chart);

        const legend = new St.BoxLayout({style_class: 'xiaoxin-legend', opacity: MUTED});
        for (const [cls, text] of [['xiaoxin-swatch-temp', 'CPU 温度'], ['xiaoxin-swatch-fan', '风扇转速']]) {
            const entry = new St.BoxLayout();
            entry.add_child(new St.Widget({style_class: `xiaoxin-swatch ${cls}`, y_align: Clutter.ActorAlign.CENTER}));
            entry.add_child(new St.Label({text}));
            legend.add_child(entry);
        }
        legend.add_child(new St.Widget({x_expand: true}));
        legend.add_child(new St.Label({text: '近 2 分钟'}));
        col.add_child(legend);

        this._note = new St.BoxLayout({style_class: 'xiaoxin-note', visible: false});
        this._noteIcon = new St.Icon({icon_size: 16, y_align: Clutter.ActorAlign.CENTER});
        this._fanGicon = fanGicon;
        this._noteLabel = new St.Label({y_align: Clutter.ActorAlign.CENTER, x_expand: true});
        this._noteLabel.clutter_text.set_line_wrap(true);
        this._note.add_child(this._noteIcon);
        this._note.add_child(this._noteLabel);
        col.add_child(this._note);

        this.add_child(col);
    }

    pushSample(s) {
        const t = s?.temps?.cpu;
        const f = s?.fans ? Math.max(...s.fans) : null;
        if (num(t) && num(f)) {
            this._temps.push(t);
            this._fans.push(f);
            if (this._temps.length > HISTORY) {
                this._temps.shift();
                this._fans.shift();
            }
        }
    }

    repaint() {
        this._chart.queue_repaint();
    }

    _paint(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        const line = (vals, lo, hi, [r, g, b], dashed) => {
            if (vals.length < 2)
                return;
            cr.setSourceRGBA(r, g, b, 1);
            cr.setLineWidth(1.8);
            cr.setDash(dashed ? [4, 3] : [], 0);
            const step = (w - 2) / (HISTORY - 1);
            vals.forEach((v, i) => {
                const x = w - 1 - (vals.length - 1 - i) * step;
                const y = h - 2 - (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo) * (h - 4);
                if (i === 0)
                    cr.moveTo(x, y);
                else
                    cr.lineTo(x, y);
            });
            cr.stroke();
        };
        line(this._temps, 30, 100, [0.91, 0.64, 0.36], false);
        line(this._fans, 0, 6100, [0.56, 0.66, 1.0], true);
        cr.$dispose();
    }

    update(s) {
        const t = s?.temps ?? {};
        const sen = Math.max(...['sen1', 'sen2'].map(k => t[k]).filter(num));
        this._tTemp.value.text = num(t.cpu) ? `${Math.round(t.cpu)}°` : '—';
        this._tTemp.unit.text = Number.isFinite(sen) ? ` / ${Math.round(sen)}°` : '';
        this._setHot(this._tTemp.value, num(t.cpu) && t.cpu >= 95);
        this._setHot(this._tTemp.unit, (Number.isFinite(sen) && sen >= 80) || !!s?.power_cap);

        const fan = fanReading(s);
        this._tFan.value.text = fan.value;
        this._tFan.unit.text = fan.unit ? ` ${fan.unit}` : '';

        const w = s?.pkg_w;
        this._tPower.value.text = num(w) ? (w < 10 ? w.toFixed(1) : `${Math.round(w)}`) : '—';
        this._tPower.unit.text = s?.power_cap ? ` / ${s.power_cap} W` : ' W';
        this._setHot(this._tPower.unit, !!s?.power_cap);

        const note = s ? statusNote(s) : {warn: true, icon: 'dialog-warning-symbolic',
            text: '守护进程未运行：sudo systemctl start xiaoxin-fand'};
        this._note.visible = !!note;
        if (note) {
            this._noteLabel.text = note.text;
            if (note.icon)
                this._noteIcon.icon_name = note.icon;
            else
                this._noteIcon.gicon = this._fanGicon;
            if (note.warn)
                this._note.add_style_class_name('xiaoxin-note-warning');
            else
                this._note.remove_style_class_name('xiaoxin-note-warning');
        }
    }

    _setHot(label, hot) {
        if (hot)
            label.add_style_class_name('xiaoxin-hot');
        else
            label.remove_style_class_name('xiaoxin-hot');
    }
});

const FanToggle = GObject.registerClass({
    Signals: {'preset-changed': {param_types: [GObject.TYPE_STRING]}},
}, class FanToggle extends QuickMenuToggle {
    _init(extension) {
        const gicon = Gio.icon_new_for_string(`${extension.path}/${FAN_ICON}`);
        super._init({title: '性能模式', gicon, toggleMode: false});
        this._path = extension.path;
        this._status = null;
        this._presets = {};
        this._items = new Map();
        this._lastOn = DEFAULT_ON_PRESET;

        this.menu.setHeader(this.gicon, '性能模式', '');
        this._presetSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._presetSection);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._statusItem = new StatusItem(gicon);
        this.menu.addMenuItem(this._statusItem);
        this._menuOpenId = this.menu.connect('open-state-changed', (_m, open) => {
            if (open)
                this._statusItem.repaint();
        });

        // 点开关本体：原厂 ↔ 上次使用的自定义预设
        this.connect('clicked', () => {
            const cur = this._status?.preset;
            this._setPreset(cur && cur !== 'stock' ? 'stock' : this._lastOn);
        });

        this._proxy = new FanProxy(Gio.DBus.system, BUS_NAME, OBJECT_PATH, (proxy, error) => {
            if (error) {
                console.warn(`xiaoxin-fan: ${error.message}`);
                this._sync();
                return;
            }
            this._signalId = proxy.connectSignal('StatusChanged', (_p, _sender, [json]) => {
                this._status = JSON.parse(json);
                this._statusItem.pushSample(this._status);
                if (this.menu.isOpen)
                    this._statusItem.repaint();
                this._sync();
            });
            this._ownerId = proxy.connect('notify::g-name-owner', () => this._refresh());
            this._refresh();
        }, null, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES);
        this._sync();
    }

    _refresh() {
        if (!this._proxy.g_name_owner) {
            this._status = null;
            this._sync();
            return;
        }
        this._proxy.ListPresetsRemote(([json], error) => {
            if (error)
                return;
            this._presets = JSON.parse(json);
            this._buildItems();
            this._proxy.GetStatusRemote(([s], e2) => {
                if (!e2) {
                    this._status = JSON.parse(s);
                    this._sync();
                }
            });
        });
    }

    _buildItems() {
        this._presetSection.removeAll();
        this._items.clear();
        for (const [id, p] of Object.entries(this._presets)) {
            const item = new PopupMenu.PopupImageMenuItem(p.label, presetIcon(this._path, id));
            const hint = new St.Label({
                text: presetHint(p), style_class: 'xiaoxin-hint', opacity: MUTED,
                x_expand: true, x_align: Clutter.ActorAlign.END, y_align: Clutter.ActorAlign.CENTER,
            });
            item.insert_child_above(hint, item.label);      // 图标 · 名称 · 要点 · 勾选
            item.connect('activate', () => this._setPreset(id));
            this._items.set(id, item);
            this._presetSection.addMenuItem(item);
        }
        this._sync();
    }

    _setPreset(id) {
        this._proxy.SetPresetRemote(id, (_result, error) => {
            if (error) {
                const msg = error.message.includes('AccessDenied')
                    ? '没有权限：切换预设需要管理员（sudo 组）账户'
                    : error.message;
                Main.notifyError('性能模式', msg);
            }
        });
    }

    _sync() {
        const s = this._status;
        const online = !!this._proxy?.g_name_owner && !!s;
        this.reactive = online;
        this._statusItem.update(online ? s : null);
        if (!online) {
            this.subtitle = '守护进程未运行';
            this.checked = false;
            this.menu.setHeader(this.gicon, '性能模式', '未连接 xiaoxin-fand');
            return;
        }

        const label = this._presets[s.preset]?.label ?? s.label;
        this.checked = s.preset !== 'stock';
        if (s.preset !== this._shownPreset) {
            this._shownPreset = s.preset;
            this.emit('preset-changed', s.preset);
        }
        if (this.checked)
            this._lastOn = s.preset;
        const fan = fanReading(s);
        this.subtitle = `${label} · ${fan.value}${fan.unit ? ` ${fan.unit}` : ''}`;

        for (const [id, item] of this._items)
            item.setOrnament(id === s.preset ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);

        this.menu.setHeader(this.gicon, `性能模式 · ${label}`, this._presets[s.preset]?.description ?? '');
    }

    destroy() {
        if (this._proxy) {
            if (this._signalId)
                this._proxy.disconnectSignal(this._signalId);
            if (this._ownerId)
                this._proxy.disconnect(this._ownerId);
            this._proxy = null;
        }
        if (this._menuOpenId)
            this.menu.disconnect(this._menuOpenId);
        super.destroy();
    }
});

const FanIndicator = GObject.registerClass(
class FanIndicator extends SystemIndicator {
    _init(extension) {
        super._init();
        this._indicator = this._addIndicator();
        this._toggle = new FanToggle(extension);
        // 顶栏小图标：仅在启用自定义预设时显示；极客显示闪电
        const fanIcon = Gio.icon_new_for_string(`${extension.path}/${FAN_ICON}`);
        const geekIcon = presetIcon(extension.path, 'geek');
        this._indicator.gicon = fanIcon;
        this._toggle.connect('preset-changed', (_t, id) => {
            this._indicator.gicon = id === 'geek' ? geekIcon : fanIcon;
        });
        this._toggle.bind_property('checked', this._indicator, 'visible', GObject.BindingFlags.SYNC_CREATE);
        this.quickSettingsItems.push(this._toggle);
    }

    destroy() {
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

// 让某个 actor 保持隐藏：GNOME 自己的代码每次同步都会重设 visible，这里随时再藏回去
class ForceHidden {
    constructor(actor, restore) {
        this._actor = actor;
        this._restore = restore;
        this._id = actor.connect('notify::visible', () => {
            if (actor.visible)
                actor.hide();
        });
        actor.hide();
    }

    destroy() {
        this._actor.disconnect(this._id);
        this._restore();
    }
}

// 系统自带的「性能模式」（Power Mode）开关与本扩展功能重叠（守护进程双向同步电源模式），守护进程在线时把它隐藏；
// 守护进程不在或扩展被禁用时恢复显示。只影响显示，power-profiles-daemon 照常运行。
class PowerModeHider {
    constructor(ownToggle) {
        this._own = ownToggle;
        this._want = false;
        this._hidden = [];
        this._retryId = 0;
        // 快捷设置是异步搭建的，系统开关可能晚于本扩展出现：每次打开菜单时再检查一次
        this._qs = Main.panel.statusArea.quickSettings;
        this._menuId = this._qs.menu.connect('open-state-changed', () => this._apply());
    }

    setHidden(hide) {
        this._want = hide;
        this._apply();
    }

    _apply() {
        if (this._want === (this._hidden.length > 0))
            return;
        if (!this._want) {
            this._stopRetry();
            this._hidden.forEach(h => h.destroy());
            this._hidden = [];
            return;
        }
        const pp = this._qs._powerProfiles;
        const toggle = pp?._powerProfilesToggle;
        if (!pp || !toggle) {
            // 还没创建好：每秒重试，最多 30 次；仍找不到就保留系统开关（GNOME 内部结构可能变了）
            if (!this._retryId) {
                let tries = 0;
                this._retryId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
                    this._apply();
                    if (this._hidden.length > 0 || !this._want || ++tries >= 30) {
                        if (this._want && this._hidden.length === 0)
                            console.warn('xiaoxin-fan: 找不到系统电源模式开关，保留显示');
                        this._retryId = 0;
                        return GLib.SOURCE_REMOVE;
                    }
                    return GLib.SOURCE_CONTINUE;
                });
            }
            return;
        }
        this._stopRetry();
        console.log('xiaoxin-fan: 已隐藏系统电源模式开关');
        // 把本扩展的开关挪到系统开关原来的位置（快捷设置按子项顺序排版）
        const grid = toggle.get_parent();
        if (grid && grid === this._own.get_parent())
            grid.set_child_below_sibling(this._own, toggle);
        this._hidden.push(
            new ForceHidden(toggle, () => toggle._sync?.()),
            new ForceHidden(pp, () => pp._syncIndicatorsVisible?.()));
    }

    _stopRetry() {
        if (this._retryId) {
            GLib.source_remove(this._retryId);
            this._retryId = 0;
        }
    }

    destroy() {
        this.setHidden(false);
        this._qs.menu.disconnect(this._menuId);
    }
}

export default class XiaoxinFanExtension extends Extension {
    enable() {
        this._indicator = new FanIndicator(this);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);

        const toggle = this._indicator._toggle;
        this._hider = new PowerModeHider(toggle);
        // 开关可点击 ⇔ 已连上守护进程
        this._reactiveId = toggle.connect('notify::reactive', () => this._hider.setHidden(toggle.reactive));
        this._hider.setHidden(toggle.reactive);
    }

    disable() {
        this._indicator?._toggle.disconnect(this._reactiveId);
        this._hider?.destroy();
        this._hider = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
