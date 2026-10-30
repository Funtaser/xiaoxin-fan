// SPDX-License-Identifier: GPL-3.0-or-later
// 小新风扇控制：快捷设置面板，前端连接系统总线上的 xiaoxin-fand。
import Gio from 'gi://Gio';
import GObject from 'gi://GObject';

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
const REASONS = {
    'curve': '按曲线', 'ec-idle': '低温交给 EC', 'stock': 'EC 自动',
    'sensor-fault': '传感器异常，已交还 EC', 'error': '出错，已交还 EC',
};
const DEFAULT_ON_PRESET = 'balanced';    // 从「原厂」点亮开关时切到的预设（首次）
const FAN_ICON = 'icons/xiaoxin-fan-symbolic.svg';

// 'icons/...' 为扩展自带图标，其余为系统图标名
function presetIcon(path, id) {
    const icon = PRESET_ICONS[id] ?? 'emblem-system-symbolic';
    return icon.startsWith('icons/') ? Gio.icon_new_for_string(`${path}/${icon}`) : icon;
}

function reasonText(r) {
    if (r?.startsWith('guard:'))
        return `安全保护（${r.slice(6)}）`;
    return REASONS[r] ?? r ?? '';
}

function fansText(s) {
    if (!s?.fans)
        return '—';
    const [a, b] = s.fans;
    return a === 0 && b === 0 ? '停转' : `${a} / ${b} rpm`;
}

const FanToggle = GObject.registerClass({
    Signals: {'preset-changed': {param_types: [GObject.TYPE_STRING]}},
}, class FanToggle extends QuickMenuToggle {
    _init(extension) {
        super._init({
            title: '风扇',
            gicon: Gio.icon_new_for_string(`${extension.path}/${FAN_ICON}`),
            toggleMode: false,
        });
        this._path = extension.path;
        this._status = null;
        this._presets = {};
        this._items = new Map();
        this._lastOn = DEFAULT_ON_PRESET;

        this.menu.setHeader(this.gicon, '风扇控制', '');
        this._presetSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._presetSection);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._infoItem = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this._infoItem.label.clutter_text.set_line_wrap(false);
        this.menu.addMenuItem(this._infoItem);

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
                Main.notifyError('风扇控制', msg);
            }
        });
    }

    _sync() {
        const s = this._status;
        const online = !!this._proxy?.g_name_owner && !!s;
        this.reactive = online;
        if (!online) {
            this.subtitle = '守护进程未运行';
            this.checked = false;
            this.menu.setHeader(this.gicon, '风扇控制', '未连接 xiaoxin-fand');
            this._infoItem.label.text = 'sudo systemctl start xiaoxin-fand';
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
        this.subtitle = `${label} · ${fansText(s)}`;

        for (const [id, item] of this._items) {
            item.setOrnament(id === s.preset ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        }

        const desc = this._presets[s.preset]?.description ?? '';
        this.menu.setHeader(this.gicon, `风扇 · ${label}`, desc);

        const t = s.temps ?? {};
        const deg = v => (typeof v === 'number' ? `${Math.round(v)}°C` : '—');
        const lines = [
            `CPU ${deg(t.cpu)}   主板 ${deg(t.sen1)}   硬盘 ${deg(t.nvme0)}`,
            `风扇 ${fansText(s)}   ${s.target ? `目标 ${s.target}` : ''}`,
            `${reasonText(s.reason)}${typeof s.pkg_w === 'number' ? `   功耗 ${s.pkg_w.toFixed(1)} W` : ''}`,
        ];
        if (s.error)
            lines.push(`错误：${s.error}`);
        this._infoItem.label.text = lines.join('\n');
    }

    destroy() {
        if (this._proxy) {
            if (this._signalId)
                this._proxy.disconnectSignal(this._signalId);
            if (this._ownerId)
                this._proxy.disconnect(this._ownerId);
            this._proxy = null;
        }
        super.destroy();
    }
});

const FanIndicator = GObject.registerClass(
class FanIndicator extends SystemIndicator {
    _init(extension) {
        super._init();
        this._indicator = this._addIndicator();
        this._toggle = new FanToggle(extension);
        // 顶栏小图标：仅在启用自定义预设时显示
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

export default class XiaoxinFanExtension extends Extension {
    enable() {
        this._indicator = new FanIndicator(this);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
