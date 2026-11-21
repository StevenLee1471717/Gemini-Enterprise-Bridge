"""Built-in local Web Console with Left-Sidebar Navigation, Seat Monitor, Public Gateway, Session/Audit Center & Usage Ledger served at http://127.0.0.1:8000/."""

from __future__ import annotations

from pathlib import Path

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Gemini Enterprise · 本地管理总台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6fb;
      --sidebar-bg: #ffffff;
      --surface: #ffffff;
      --surface-soft: #f8fafd;
      --surface-muted: #f1f5fb;
      --border: #e4eaf3;
      --border-strong: #cdd8ea;
      --text: #0f172a;
      --text-sub: #334155;
      --muted: #64748b;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --primary-soft: #eff6ff;
      --indigo: #4f46e5;
      --teal: #0d9488;
      --teal-soft: #f0fdfa;
      --ok: #059669;
      --ok-soft: #ecfdf5;
      --ok-border: #a7f3d0;
      --warn: #d97706;
      --warn-soft: #fffbeb;
      --warn-border: #fde68a;
      --err: #e11d48;
      --err-soft: #fff1f2;
      --err-border: #fecdd3;
      --shadow-xs: 0 1px 3px rgba(15, 23, 42, 0.04);
      --shadow-card: 0 8px 24px -12px rgba(15, 23, 42, 0.06), 0 2px 6px rgba(15, 23, 42, 0.025);
      --shadow-hover: 0 14px 30px -12px rgba(37, 99, 235, 0.13), 0 4px 10px rgba(15, 23, 42, 0.04);
      --radius-xl: 18px;
      --radius-lg: 14px;
      --radius-md: 10px;
    }

    * { box-sizing: border-box; }
    [hidden] { display: none !important; }

    html, body {
      margin: 0;
      padding: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "SF Pro Text", "Segoe UI", system-ui, sans-serif;
      background:
        radial-gradient(960px 440px at 18% -8%, rgba(37, 99, 235, 0.055), transparent 62%),
        radial-gradient(760px 360px at 96% -4%, rgba(13, 148, 136, 0.045), transparent 58%),
        var(--bg);
      color: var(--text);
      font-size: 14px;
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
      font-variant-numeric: tabular-nums;
    }

    /* App Shell: Left Sidebar + Right Workspace */
    .app-shell {
      display: grid;
      grid-template-columns: 256px minmax(0, 1fr);
      min-height: 100vh;
    }

    .sidebar {
      position: sticky;
      top: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      background: var(--sidebar-bg);
      border-right: 1px solid var(--border);
      padding: 20px 14px 16px;
      overflow-y: auto;
      z-index: 30;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 4px 8px 16px;
      border-bottom: 1px solid #eef2f8;
      margin-bottom: 14px;
    }

    .brand-logo {
      width: 38px;
      height: 38px;
      border-radius: 11px;
      background: linear-gradient(135deg, #2563eb 0%, #4f46e5 100%);
      color: #ffffff;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 8px 18px -6px rgba(37, 99, 235, 0.45);
      flex-shrink: 0;
    }

    .brand h1 {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 800;
      letter-spacing: -0.015em;
      color: var(--text);
      line-height: 1.25;
    }

    .brand-sub {
      margin-top: 2px;
      font-size: 0.76rem;
      color: var(--muted);
      font-weight: 500;
    }

    .side-group {
      margin-bottom: 14px;
    }

    .side-group-label {
      padding: 0 10px;
      margin-bottom: 6px;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      color: #94a3b8;
      text-transform: uppercase;
    }

    .side-nav-list {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .side-nav-btn {
      width: 100%;
      border: 1px solid transparent;
      background: transparent;
      border-radius: 11px;
      padding: 9px 11px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font: inherit;
      font-size: 0.88rem;
      font-weight: 600;
      color: var(--text-sub);
      cursor: pointer;
      text-align: left;
      transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
    }

    .side-nav-btn:hover {
      background: var(--surface-soft);
      color: var(--text);
    }

    .side-nav-btn.active {
      background: var(--primary-soft);
      border-color: rgba(37, 99, 235, 0.18);
      color: var(--primary);
      font-weight: 700;
      box-shadow: 0 2px 8px -3px rgba(37, 99, 235, 0.14);
    }

    .side-nav-left {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }

    .side-nav-icon {
      width: 26px;
      height: 26px;
      border-radius: 8px;
      background: var(--surface-muted);
      color: var(--muted);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.86rem;
      flex-shrink: 0;
      transition: background 0.15s ease, color 0.15s ease;
    }

    .side-nav-btn.active .side-nav-icon {
      background: #dbeafe;
      color: var(--primary);
    }

    .side-nav-title {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .side-badge {
      display: inline-flex;
      align-items: center;
      height: 20px;
      padding: 0 7px;
      border-radius: 999px;
      font-size: 0.71rem;
      font-weight: 650;
      background: var(--surface-muted);
      color: var(--muted);
      white-space: nowrap;
      flex-shrink: 0;
    }
    .side-badge.ok { background: var(--ok-soft); color: var(--ok); }
    .side-badge.busy { background: #dbeafe; color: var(--primary); }
    .side-badge.warn { background: var(--warn-soft); color: var(--warn); }

    .sidebar-foot {
      margin-top: auto;
      padding-top: 14px;
      border-top: 1px solid #eef2f8;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    .sidebar-host-card {
      padding: 10px 12px;
      border-radius: 12px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
      font-size: 0.77rem;
    }

    .sidebar-host-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      font-weight: 700;
      color: var(--text);
      margin-bottom: 3px;
    }

    .sidebar-host-sub {
      color: var(--muted);
      font-size: 0.73rem;
      word-break: break-all;
    }

    /* Main Workspace */
    .main-stage {
      min-width: 0;
      display: flex;
      flex-direction: column;
      min-height: 100vh;
    }

    .topbar {
      position: sticky;
      top: 0;
      z-index: 20;
      background: rgba(244, 246, 251, 0.86);
      backdrop-filter: blur(10px);
      border-bottom: 1px solid rgba(228, 234, 243, 0.9);
      padding: 14px 28px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
    }

    .page-heading h2 {
      margin: 0;
      font-size: 1.2rem;
      font-weight: 800;
      letter-spacing: -0.015em;
      color: var(--text);
    }

    .page-heading p {
      margin: 2px 0 0;
      font-size: 0.82rem;
      color: var(--muted);
    }

    .topbar-actions {
      display: flex;
      align-items: center;
      gap: 9px;
      flex-wrap: wrap;
    }

    .page-container {
      width: 100%;
      max-width: 1280px;
      margin: 0 auto;
      padding: 22px 28px 44px;
    }

    @keyframes pageFadeIn {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .page-panel {
      animation: pageFadeIn 0.16s ease;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    /* Live Pill & Status Dots */
    .live-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      height: 34px;
      padding: 0 13px;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 600;
      background: var(--surface);
      border: 1px solid var(--border);
      box-shadow: var(--shadow-xs);
      white-space: nowrap;
      transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }
    .live-pill.flash {
      border-color: rgba(37, 99, 235, 0.36);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--warn);
      box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.16);
      flex-shrink: 0;
    }
    .dot.ok { background: var(--ok); box-shadow: 0 0 0 3px rgba(5, 150, 105, 0.16); }
    .dot.err { background: var(--err); box-shadow: 0 0 0 3px rgba(225, 29, 72, 0.16); }

    /* Buttons */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      height: 34px;
      padding: 0 14px;
      border-radius: 9px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text-sub);
      font: inherit;
      font-size: 0.83rem;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
      user-select: none;
      transition: transform 0.12s ease, background 0.15s ease, border-color 0.15s ease, color 0.15s ease, box-shadow 0.15s ease;
      text-decoration: none;
    }
    .btn:hover:not(:disabled) {
      border-color: var(--border-strong);
      background: var(--surface-soft);
      color: var(--text);
    }
    .btn:active:not(:disabled) {
      transform: scale(0.97);
    }
    .btn:disabled {
      opacity: 0.52;
      cursor: not-allowed;
    }
    .btn.primary {
      background: linear-gradient(135deg, #2563eb, #1d4ed8);
      border-color: transparent;
      color: #ffffff;
      box-shadow: 0 6px 16px -6px rgba(37, 99, 235, 0.42);
    }
    .btn.primary:hover:not(:disabled) {
      background: linear-gradient(135deg, #1d4ed8, #1e40af);
    }
    .btn.soft {
      background: var(--primary-soft);
      border-color: rgba(37, 99, 235, 0.2);
      color: var(--primary);
    }
    .btn.soft:hover:not(:disabled) {
      background: #dbeafe;
    }
    .btn.danger-soft {
      background: var(--err-soft);
      border-color: var(--err-border);
      color: var(--err);
    }
    .btn.danger-soft:hover:not(:disabled) {
      background: #ffe4e6;
    }
    .btn.danger {
      background: linear-gradient(135deg, #ef4444, #dc2626);
      border-color: transparent;
      color: #ffffff;
      box-shadow: 0 6px 16px -6px rgba(220, 38, 38, 0.42);
    }
    .btn.danger:hover:not(:disabled) {
      background: linear-gradient(135deg, #dc2626, #b91c1c);
    }
    .btn.sm {
      height: 28px;
      padding: 0 10px;
      font-size: 0.77rem;
      border-radius: 8px;
    }
    .btn.xs {
      height: 24px;
      padding: 0 8px;
      font-size: 0.73rem;
      border-radius: 7px;
    }

    @keyframes refreshSpin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    .btn-refresh { min-width: 84px; }
    .refresh-icon {
      width: 14px;
      height: 14px;
      flex-shrink: 0;
      transition: transform 0.25s ease;
    }
    .btn-refresh:hover:not(:disabled):not(.is-refreshing) .refresh-icon { transform: rotate(35deg); }
    .btn-refresh.is-refreshing {
      border-color: rgba(37, 99, 235, 0.38);
      background: var(--primary-soft);
      color: var(--primary);
    }
    .btn-refresh.is-refreshing .refresh-icon { animation: refreshSpin 0.56s linear infinite; }
    .btn-refresh.is-refreshed { border-color: var(--ok-border); background: var(--ok-soft); color: var(--ok); }
    .btn-refresh.is-error { border-color: var(--err-border); background: var(--err-soft); color: var(--err); }

    /* Chips */
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      height: 22px;
      padding: 0 9px;
      border-radius: 999px;
      font-size: 0.74rem;
      font-weight: 600;
      white-space: nowrap;
      flex-shrink: 0;
      background: var(--surface-muted);
      color: var(--text-sub);
    }
    .chip.ok { background: var(--ok-soft); color: var(--ok); }
    .chip.busy { background: var(--primary-soft); color: var(--primary); }
    .chip.warn { background: var(--warn-soft); color: var(--warn); }
    .chip.err { background: var(--err-soft); color: var(--err); }
    .chip.purple { background: #eef2ff; color: var(--indigo); }

    /* Cards & Containers */
    .surface-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-xl);
      box-shadow: var(--shadow-card);
      padding: 20px 22px;
    }

    .card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }

    .seat-cap-toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .seat-cap-group {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      padding: 3px 4px 3px 10px;
      border-radius: 999px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
    }

    .seat-cap-label {
      font-size: 0.75rem;
      font-weight: 600;
      color: var(--muted);
      margin-right: 3px;
      user-select: none;
    }

    .seat-cap-btn {
      border: none;
      border-radius: 999px;
      height: 23px;
      padding: 0 8px;
      background: transparent;
      color: var(--muted);
      font: inherit;
      font-size: 0.75rem;
      font-weight: 650;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .seat-cap-btn:hover:not(:disabled) { color: var(--text); background: #eaf0fa; }
    .seat-cap-btn.active { background: #ffffff; color: var(--primary); box-shadow: 0 1px 4px rgba(15, 23, 42, 0.1); }
    .seat-cap-btn:disabled { opacity: 0.55; cursor: wait; }

    .seat-cap-custom-wrap {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      margin-left: 2px;
    }
    .seat-cap-custom-wrap[hidden] { display: none !important; }

    .seat-cap-input {
      width: 60px;
      height: 23px;
      padding: 0 7px;
      border-radius: 999px;
      border: 1px solid #cbd5e1;
      background: #ffffff;
      color: var(--text);
      font: inherit;
      font-size: 0.75rem;
      font-weight: 650;
      text-align: center;
      outline: none;
      -moz-appearance: textfield;
    }
    .seat-cap-input::-webkit-outer-spin-button,
    .seat-cap-input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
    .seat-cap-input:focus { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15); }

    .seat-cap-apply-btn {
      border: none;
      border-radius: 999px;
      height: 23px;
      padding: 0 9px;
      background: var(--primary);
      color: #ffffff;
      font: inherit;
      font-size: 0.72rem;
      font-weight: 650;
      cursor: pointer;
      transition: opacity 0.15s ease;
      white-space: nowrap;
    }
    .seat-cap-apply-btn:hover:not(:disabled) { opacity: 0.9; }
    .seat-cap-apply-btn:disabled { opacity: 0.55; cursor: wait; }

    .card-title-group {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      flex-wrap: wrap;
    }

    .card-title {
      margin: 0;
      font-size: 1rem;
      font-weight: 750;
      color: var(--text);
    }

    .card-subtitle {
      font-size: 0.8rem;
      color: var(--muted);
    }

    .sec-icon {
      width: 30px;
      height: 30px;
      border-radius: 9px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.92rem;
      flex-shrink: 0;
      background: var(--primary-soft);
      color: var(--primary);
    }
    .sec-icon.green { background: var(--ok-soft); color: var(--ok); }
    .sec-icon.indigo { background: #eef2ff; color: var(--indigo); }
    .sec-icon.teal { background: var(--teal-soft); color: var(--teal); }
    .sec-icon.amber { background: var(--warn-soft); color: var(--warn); }
    .sec-icon.slate { background: var(--surface-muted); color: var(--muted); }

    /* 3-Step Self-Check Strip */
    .check-strip {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      align-items: stretch;
    }

    .step-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      min-height: 58px;
      padding: 11px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      box-shadow: var(--shadow-xs);
    }
    .step-card.ok { border-color: rgba(5, 150, 105, 0.28); }
    .step-card.warn { border-color: var(--warn-border); background: #fffdf7; }
    .step-card.err { border-color: var(--err-border); background: #fffafa; }
    .step-left { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
    .step-text { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
    .step-right { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
    .step-badge {
      width: 25px;
      height: 25px;
      border-radius: 8px;
      font-size: 0.76rem;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      background: var(--surface-muted);
      color: var(--muted);
    }
    .step-card.ok .step-badge { background: var(--ok-soft); color: var(--ok); }
    .step-card.warn .step-badge { background: var(--warn-soft); color: var(--warn); }
    .step-card.err .step-badge { background: var(--err-soft); color: var(--err); }
    .step-title { font-size: 0.86rem; font-weight: 700; color: var(--text); white-space: nowrap; }
    .step-desc { font-size: 0.78rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

    /* Overview Quick Summary Cards */
    .overview-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    .ov-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 5px;
      box-shadow: var(--shadow-xs);
      cursor: pointer;
      text-align: left;
      font: inherit;
      transition: transform 0.14s ease, border-color 0.14s ease, box-shadow 0.14s ease;
    }
    .ov-card:hover {
      border-color: #bfd2f2;
      box-shadow: var(--shadow-hover);
      transform: translateY(-1px);
    }
    .ov-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 0.77rem;
      font-weight: 650;
      color: var(--muted);
    }
    .ov-val {
      font-size: 1.18rem;
      font-weight: 800;
      color: var(--text);
      letter-spacing: -0.015em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .ov-sub {
      font-size: 0.75rem;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    /* Seat Detail Panel & 5-Column Seat Grid */
    .seat-detail-panel {
      border-radius: var(--radius-lg);
      background: linear-gradient(180deg, #f8faff 0%, #f3f7fe 100%);
      border: 1px solid #dfe8f7;
      margin-bottom: 14px;
    }
    .seat-detail-empty {
      padding: 11px 15px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      font-size: 0.82rem;
      color: var(--muted);
    }
    .seat-detail-empty-left {
      display: flex;
      align-items: center;
      gap: 9px;
      min-width: 0;
    }
    .seat-detail-hint-dot {
      width: 22px;
      height: 22px;
      border-radius: 7px;
      background: #e6effd;
      color: var(--primary);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.76rem;
      font-weight: 700;
      flex-shrink: 0;
    }
    .seat-detail-content {
      padding: 16px 18px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .seat-detail-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .seat-detail-title-group {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      flex: 1;
      flex-wrap: wrap;
    }
    .seat-detail-badge {
      padding: 3px 9px;
      border-radius: 7px;
      background: var(--primary);
      color: #ffffff;
      font-size: 0.76rem;
      font-weight: 700;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .seat-detail-title {
      font-size: 0.94rem;
      font-weight: 750;
      color: var(--text);
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 480px;
    }
    .seat-detail-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
    .ctx-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 0.8rem;
      color: var(--text-sub);
    }
    .ctx-track {
      width: 100%;
      height: 8px;
      border-radius: 999px;
      background: #e4ecf7;
      overflow: hidden;
      margin-top: 6px;
    }
    .ctx-fill {
      height: 100%;
      width: 0%;
      border-radius: 999px;
      background: linear-gradient(90deg, #10b981, #2563eb);
      transition: width 0.3s ease;
    }
    .ctx-fill.warn { background: linear-gradient(90deg, #f59e0b, #ea580c); }
    .ctx-fill.danger { background: linear-gradient(90deg, #ef4444, #e11d48); }

    .seat-detail-kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .detail-kpi {
      padding: 10px 12px;
      border-radius: 11px;
      background: #ffffff;
      border: 1px solid #e3ebf8;
      display: flex;
      flex-direction: column;
      gap: 3px;
      min-width: 0;
    }
    .detail-kpi .k { font-size: 0.74rem; font-weight: 600; color: var(--muted); white-space: nowrap; }
    .detail-kpi .v { font-size: 0.85rem; font-weight: 700; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .detail-kpi .s { font-size: 0.74rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

    .seats-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      align-items: stretch;
    }
    .seat-card {
      background: var(--surface-soft);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 13px;
      display: flex;
      flex-direction: column;
      gap: 9px;
      min-width: 0;
      cursor: pointer;
      user-select: none;
      transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease, background 0.15s ease;
    }
    .seat-card:hover {
      background: #ffffff;
      box-shadow: var(--shadow-hover);
      border-color: #c5d5ef;
      transform: translateY(-1px);
    }
    .seat-card:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
    .seat-card.busy {
      background: linear-gradient(165deg, #eff6ff 0%, #f8fbff 100%);
      border-color: rgba(37, 99, 235, 0.42);
      box-shadow: 0 10px 24px -14px rgba(37, 99, 235, 0.25);
    }
    .seat-card.active-idle { background: #ffffff; border-color: #d6e1f3; }
    .seat-card.selected {
      background: #ffffff;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14), 0 12px 26px -14px rgba(37, 99, 235, 0.28);
    }
    @keyframes seatPulse {
      0%, 100% { transform: scale(1); opacity: 1; }
      50% { transform: scale(1.32); opacity: 0.55; }
    }
    .live-work-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--primary);
      display: inline-block;
      animation: seatPulse 1.35s ease-in-out infinite;
    }
    .seat-top { display: flex; justify-content: space-between; align-items: center; gap: 6px; height: 24px; min-width: 0; }
    .seat-name { font-weight: 700; font-size: 0.85rem; color: var(--text); display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
    .seat-topic {
      height: 46px;
      padding: 7px 9px;
      border-radius: 9px;
      background: rgba(241, 245, 251, 0.78);
      border: 1px solid #e9eef8;
      font-size: 0.81rem;
      font-weight: 650;
      line-height: 16px;
      color: var(--text);
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      word-break: break-word;
    }
    .seat-card.busy .seat-topic { background: rgba(255, 255, 255, 0.9); border-color: rgba(37, 99, 235, 0.2); color: #1e3a8a; }
    .seat-topic.empty { color: var(--muted); font-weight: 500; background: transparent; border-style: dashed; }
    .seat-meta { display: flex; justify-content: space-between; align-items: center; gap: 8px; height: 18px; font-size: 0.78rem; color: var(--muted); white-space: nowrap; }
    .seat-meta span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .seat-mini-track { width: 100%; height: 5px; border-radius: 999px; background: #e6edf7; overflow: hidden; }
    .seat-mini-fill { height: 100%; width: 0%; border-radius: 999px; background: linear-gradient(90deg, #10b981, #3b82f6); }

    .idle-fold-wrap {
      margin-top: 14px;
      border-radius: 13px;
      border: 1px solid var(--border);
      background: #fafcff;
      overflow: hidden;
    }
    .idle-fold-btn {
      width: 100%;
      border: none;
      background: transparent;
      padding: 11px 15px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      font: inherit;
      font-size: 0.83rem;
      font-weight: 650;
      color: var(--text-sub);
      cursor: pointer;
    }
    .idle-fold-btn:hover { background: #f1f6fd; }
    .idle-fold-hint { font-size: 0.77rem; font-weight: 600; color: var(--muted); }
    .idle-fold-body { padding: 12px 15px 15px; border-top: 1px dashed var(--border); }
    .empty-seats {
      grid-column: 1 / -1;
      text-align: center;
      padding: 20px;
      border-radius: 13px;
      background: var(--surface-soft);
      border: 1px dashed var(--border-strong);
      color: var(--muted);
      font-size: 0.85rem;
    }

    /* Forms & Inputs */
    select.clean-select,
    input.clean-input,
    textarea.clean-textarea {
      padding: 8px 12px;
      border-radius: 9px;
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--text);
      font: inherit;
      font-size: 0.85rem;
      outline: none;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    select.clean-select,
    input.clean-input {
      height: 36px;
      padding: 0 12px;
    }
    select.clean-select:focus,
    input.clean-input:focus,
    textarea.clean-textarea:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
    }

    /* Public Gateway & API Keys (Redesigned Clean Layout) */
    .gateway-hero {
      border-radius: 16px;
      background: linear-gradient(135deg, #f8fbff 0%, #f3fbf9 100%);
      border: 1px solid #d5e6f7;
      padding: 18px 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .gateway-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .gateway-endpoint-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 16px;
      border-radius: 12px;
      background: #ffffff;
      border: 1px solid #d7e3f4;
      box-shadow: var(--shadow-xs);
      flex-wrap: wrap;
    }
    .gateway-endpoint-meta { min-width: 0; flex: 1; }
    .gateway-endpoint-label {
      font-size: 0.74rem;
      font-weight: 650;
      color: var(--muted);
      margin-bottom: 2px;
    }
    .tunnel-url-val {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.94rem;
      font-weight: 700;
      color: var(--text);
      word-break: break-all;
    }
    .gateway-foot-note {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 0.8rem;
      color: var(--muted);
    }
    .tunnel-probe-box {
      padding: 10px 14px;
      border-radius: 10px;
      font-size: 0.81rem;
      background: #ffffff;
      border: 1px solid var(--border);
      color: var(--text-sub);
    }
    .tunnel-probe-box.ok { border-color: var(--ok-border); background: var(--ok-soft); color: #065f46; }
    .tunnel-probe-box.warn { border-color: var(--warn-border); background: var(--warn-soft); color: #92400e; }

    /* Clean Collapsible Settings Drawer */
    details.settings-drawer {
      border-radius: 14px;
      border: 1px solid var(--border);
      background: var(--surface-soft);
      overflow: hidden;
    }
    details.settings-drawer > summary {
      list-style: none;
      cursor: pointer;
      padding: 12px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 0.85rem;
      font-weight: 700;
      color: var(--text-sub);
      user-select: none;
      transition: background 0.15s ease;
    }
    details.settings-drawer > summary::-webkit-details-marker { display: none; }
    details.settings-drawer > summary:hover { background: #eff4fc; }
    .drawer-chevron {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 0.78rem;
      color: var(--muted);
      white-space: nowrap;
    }
    .drawer-chevron .drawer-text-open { display: none; }
    .drawer-chevron .drawer-arrow {
      display: inline-block;
      transition: transform 0.18s ease;
    }
    details.settings-drawer[open] > summary .drawer-chevron .drawer-text-closed { display: none; }
    details.settings-drawer[open] > summary .drawer-chevron .drawer-text-open { display: inline; }
    details.settings-drawer[open] > summary .drawer-chevron .drawer-arrow { transform: rotate(180deg); }
    .settings-drawer-body {
      padding: 16px 18px;
      border-top: 1px solid var(--border);
      background: #ffffff;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .dist-form-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      align-items: end;
    }
    .create-key-panel {
      margin-bottom: 16px;
      padding: 18px 20px;
      border-radius: 15px;
      background: linear-gradient(180deg, #f8fbff 0%, #ffffff 100%);
      border: 1px solid #cfe0f8;
      box-shadow: var(--shadow-xs);
    }
    .create-grid-3 {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 12px;
    }
    .dist-field {
      display: flex;
      flex-direction: column;
      gap: 5px;
      min-width: 0;
    }
    .dist-field label {
      font-size: 0.76rem;
      font-weight: 650;
      color: var(--text-sub);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .dist-field input.clean-input,
    .dist-field select.clean-select {
      width: 100%;
    }

    .dist-share-box {
      margin-bottom: 16px;
      padding: 15px 18px;
      border-radius: 14px;
      background: linear-gradient(180deg, #f0fdfa 0%, #f8fafc 100%);
      border: 1px solid #99f6e4;
    }
    .dist-share-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 10px;
      font-size: 0.88rem;
      font-weight: 750;
      color: #0f766e;
    }
    .dist-share-pre {
      margin: 0;
      padding: 12px 14px;
      border-radius: 10px;
      background: #ffffff;
      border: 1px solid #ccfbf1;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.81rem;
      color: var(--text);
      white-space: pre-wrap;
      word-break: break-all;
      line-height: 1.55;
    }
    .dist-row-actions {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }

    /* Compact Row More-Actions Dropdown */
    .row-more-menu {
      position: relative;
      display: inline-block;
    }
    .row-more-menu > summary {
      list-style: none;
      height: 28px;
      padding: 0 9px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text-sub);
      font-size: 0.76rem;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      cursor: pointer;
      user-select: none;
    }
    .row-more-menu > summary::-webkit-details-marker { display: none; }
    .row-more-menu > summary:hover { background: var(--surface-soft); border-color: var(--border-strong); }
    .row-more-pop {
      position: fixed;
      min-width: 146px;
      padding: 6px;
      border-radius: 11px;
      background: #ffffff;
      border: 1px solid var(--border);
      box-shadow: 0 14px 32px -8px rgba(15, 23, 42, 0.18);
      display: flex;
      flex-direction: column;
      gap: 3px;
      z-index: 90;
    }
    .row-more-pop .btn {
      width: 100%;
      justify-content: flex-start;
      border-color: transparent;
      height: 30px;
      font-size: 0.78rem;
    }

    /* Segmented Sub-Tabs (Audit & Ledger) */
    .sub-tabs-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 14px;
      padding-bottom: 12px;
      border-bottom: 1px solid #eef2f8;
    }
    .seg-tabs {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px;
      border-radius: 11px;
      background: var(--surface-muted);
      border: 1px solid var(--border);
      flex-wrap: wrap;
    }
    .seg-tab {
      border: none;
      background: transparent;
      color: var(--text-sub);
      font: inherit;
      font-size: 0.83rem;
      font-weight: 650;
      height: 31px;
      padding: 0 13px;
      border-radius: 8px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      transition: all 0.14s ease;
    }
    .seg-tab:hover { color: var(--text); }
    .seg-tab.active {
      background: #ffffff;
      color: var(--primary);
      font-weight: 700;
      box-shadow: 0 2px 6px rgba(15, 23, 42, 0.07);
    }

    /* Bounded Scrollable Table Viewport + Sticky Header + Pagination */
    .table-viewport {
      max-height: 460px;
      overflow-y: auto;
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: #ffffff;
    }
    .table-viewport.compact {
      max-height: 220px;
    }
    table.clean-table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 0.83rem;
    }
    table.clean-table th,
    table.clean-table td {
      padding: 11px 13px;
      text-align: left;
      border-bottom: 1px solid #eef2f8;
      vertical-align: middle;
    }
    table.clean-table tbody tr:last-child td {
      border-bottom: none;
    }
    table.clean-table th.num,
    table.clean-table td.num {
      text-align: right;
      white-space: nowrap;
    }
    table.clean-table td.nw {
      white-space: nowrap;
    }
    table.clean-table thead th {
      position: sticky;
      top: 0;
      z-index: 5;
      color: var(--muted);
      font-size: 0.77rem;
      font-weight: 700;
      background: #f8fafd;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }
    table.clean-table tbody tr:hover td {
      background: #f9fbff;
    }
    table.clean-table tbody tr.is-expanded td {
      background: #f4f8ff;
      border-bottom-color: transparent;
    }
    table.clean-table tbody tr.detail-expand-row td {
      background: #f8fbff;
      padding: 0 16px 14px;
      border-bottom: 1px solid #e2eaf6;
    }
    .expand-detail-card {
      padding: 12px 14px;
      border-radius: 10px;
      background: #ffffff;
      border: 1px solid #dce6f5;
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 220px;
      overflow-y: auto;
      font-size: 0.81rem;
      line-height: 1.55;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .expand-detail-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      font-size: 0.75rem;
      color: var(--muted);
      border-top: 1px dashed var(--border);
      padding-top: 7px;
    }

    .one-line-summary {
      max-width: 310px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-weight: 600;
      color: var(--text);
    }
    .one-line-sub {
      max-width: 310px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-size: 0.76rem;
      color: var(--muted);
      margin-top: 2px;
    }

    /* Table Pagination Bar */
    .pager-bar {
      margin-top: 11px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 0.8rem;
      color: var(--muted);
    }
    .pager-controls {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }

    /* Quota Bars */
    .cell-two-line {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      justify-content: center;
      gap: 4px;
    }
    .cell-two-line .line-main {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      font-size: 0.83rem;
      font-weight: 650;
      color: var(--text);
      white-space: nowrap;
    }
    .cell-two-line .line-sub {
      font-size: 0.74rem;
      line-height: 16px;
      color: var(--muted);
      white-space: nowrap;
    }
    .quota-stack { display: flex; flex-direction: column; gap: 6px; width: 172px; min-width: 152px; }
    .quota-line { display: flex; flex-direction: column; gap: 4px; font-size: 0.75rem; }
    .quota-line-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; color: var(--text-sub); white-space: nowrap; }
    .quota-line-top.compact { justify-content: flex-start; gap: 6px; }
    .quota-line-val { font-weight: 650; color: var(--text); }
    .quota-bar-track { height: 6px; border-radius: 999px; background: #e8eef7; overflow: hidden; }
    .quota-bar-fill { height: 100%; border-radius: 999px; background: #2563eb; transition: width 0.25s ease; }
    .quota-bar-fill.warn { background: #f59e0b; }
    .quota-bar-fill.err { background: #ef4444; }

    /* Audit Toolbar */
    .audit-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 12px;
      padding: 10px 12px;
      border-radius: 12px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
    }
    .audit-filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1; }

    /* Ledger Hero & Charts */
    .currency-switch {
      display: inline-flex;
      background: var(--surface-muted);
      padding: 3px;
      border-radius: 999px;
      border: 1px solid var(--border);
    }
    .cur-btn {
      border: none;
      background: transparent;
      color: var(--muted);
      font: inherit;
      font-size: 0.77rem;
      font-weight: 700;
      padding: 4px 11px;
      border-radius: 999px;
      cursor: pointer;
    }
    .cur-btn.active { background: #ffffff; color: var(--primary); box-shadow: 0 1px 4px rgba(15, 23, 42, 0.08); }

    .period-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      padding: 8px 12px;
      border-radius: 12px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
    }
    .period-tabs { display: flex; gap: 4px; flex-wrap: wrap; }
    .period-tab {
      background: transparent;
      color: var(--muted);
      border: none;
      border-radius: 8px;
      height: 29px;
      padding: 0 11px;
      font: inherit;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
    }
    .period-tab:hover { color: var(--text); background: #eaf0fa; }
    .period-tab.active { background: var(--primary); color: #ffffff; box-shadow: 0 4px 10px -3px rgba(37, 99, 235, 0.35); }
    .date-picker-wrap { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 0.79rem; color: var(--muted); }
    input[type="date"].date-input {
      height: 30px;
      padding: 0 9px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--text);
      font: inherit;
      font-size: 0.79rem;
    }

    .ledger-hero {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      align-items: stretch;
    }
    .stat-tile {
      padding: 15px 16px;
      border-radius: 15px;
      background: var(--surface);
      border: 1px solid var(--border);
      box-shadow: var(--shadow-xs);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 6px;
      min-width: 0;
    }
    .stat-icon-box {
      width: 24px;
      height: 24px;
      flex-shrink: 0;
      border-radius: 7px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .stat-icon-box.emerald { background: var(--ok-soft); color: var(--ok); }
    .stat-icon-box.blue { background: var(--primary-soft); color: var(--primary); }
    .stat-icon-box.teal { background: var(--teal-soft); color: var(--teal); }
    .stat-icon-box.indigo { background: #eef2ff; color: var(--indigo); }
    .stat-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 0.79rem;
      font-weight: 650;
      color: var(--muted);
      white-space: nowrap;
    }
    .stat-big {
      font-size: 1.28rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      color: var(--text);
      margin: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .stat-sub {
      font-size: 0.76rem;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .charts-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-items: stretch;
    }
    .chart-box {
      padding: 16px 18px;
      border-radius: 15px;
      background: var(--surface);
      border: 1px solid var(--border);
      box-shadow: var(--shadow-xs);
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-width: 0;
    }
    .chart-title-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      font-size: 0.86rem;
      font-weight: 700;
      color: var(--text);
    }
    .chart-legend { display: flex; gap: 10px; font-size: 0.75rem; font-weight: 500; color: var(--muted); }
    .leg-dot { display: inline-block; width: 8px; height: 8px; border-radius: 3px; margin-right: 4px; vertical-align: middle; }
    .bars-stage {
      flex: 1;
      min-height: 142px;
      display: flex;
      align-items: flex-end;
      gap: 8px;
      padding: 10px 4px 0;
      border-bottom: 1px solid var(--border);
    }
    .bar-col {
      flex: 1;
      min-width: 0;
      height: 100%;
      display: flex;
      flex-direction: column;
      justify-content: flex-end;
      align-items: center;
      gap: 6px;
      cursor: pointer;
    }
    .bar-stack {
      width: 100%;
      max-width: 36px;
      flex: 1;
      min-height: 98px;
      max-height: 156px;
      display: flex;
      flex-direction: column;
      justify-content: flex-end;
      border-radius: 7px 7px 3px 3px;
      overflow: hidden;
      background: #eaf0f9;
      transition: transform 0.15s ease;
    }
    .bar-val { font-size: 0.71rem; font-weight: 650; color: var(--text-sub); white-space: nowrap; min-height: 15px; }
    .bar-col:hover .bar-stack { transform: translateY(-2px); }
    .bar-col.selected .bar-lbl { color: var(--primary); font-weight: 700; }
    .bar-seg-actual { background: linear-gradient(180deg, #3b82f6 0%, #2563eb 100%); width: 100%; border-radius: 6px 6px 2px 2px; transition: height 0.25s ease; }
    .bar-lbl { font-size: 0.73rem; color: var(--muted); white-space: nowrap; }

    .task-ratio-bar { width: 100%; height: 12px; border-radius: 999px; background: #e6edf7; overflow: hidden; display: flex; gap: 2px; }
    .task-list { display: flex; flex-direction: column; gap: 8px; }
    .task-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 0.82rem; }
    .task-row-left { display: flex; align-items: center; gap: 7px; color: var(--text-sub); font-weight: 600; }
    .task-row-right { color: var(--muted); font-size: 0.79rem; }

    /* Probe & Diagnostics */
    .probe-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 15px 18px;
      border-radius: var(--radius-lg);
      background: var(--surface-soft);
      border: 1px solid var(--border);
    }
    .probe-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
    .probe-result {
      margin-top: 14px;
      padding: 15px 18px;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: #ffffff;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .probe-result.ok { background: var(--ok-soft); border-color: var(--ok-border); }
    .probe-result.err { background: var(--err-soft); border-color: var(--err-border); }
    .probe-res-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; font-size: 0.9rem; font-weight: 700; }
    .probe-res-body { margin: 0; font-size: 0.87rem; color: var(--text-sub); white-space: pre-wrap; word-break: break-word; max-height: 280px; overflow: auto; }

    .diag-kv-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    .diag-kv {
      padding: 14px 16px;
      border-radius: 14px;
      background: var(--surface);
      border: 1px solid var(--border);
      box-shadow: var(--shadow-xs);
      min-width: 0;
    }
    .diag-kv .k { color: var(--muted); font-size: 0.76rem; font-weight: 600; }
    .diag-kv .v { font-weight: 700; font-size: 0.88rem; color: var(--text); margin-top: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .logs-box {
      max-height: 440px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.8rem;
      line-height: 1.6;
      background: var(--surface-soft);
      border-radius: 13px;
      padding: 14px 16px;
      border: 1px solid var(--border);
      color: var(--text-sub);
    }
    .log-item { padding: 5px 0; border-bottom: 1px solid #eaeff8; }
    .log-item:last-child { border-bottom: none; }

    /* Modals */
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.38);
      backdrop-filter: blur(4px);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
      z-index: 100;
    }
    .modal-card {
      width: 100%;
      max-width: 540px;
      background: #ffffff;
      border-radius: 20px;
      border: 1px solid var(--border);
      box-shadow: 0 24px 60px -12px rgba(15, 23, 42, 0.22);
      padding: 22px 24px;
    }
    .modal-card.wide { max-width: 680px; max-height: 90vh; overflow-y: auto; }
    .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
    .modal-head h3 { margin: 0; font-size: 1.08rem; }
    .edit-grid-2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 12px; }
    .copy-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 10px 13px;
      border-radius: 12px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
      margin-bottom: 9px;
    }
    .copy-meta { min-width: 0; }
    .copy-label { font-size: 0.75rem; color: var(--muted); }
    .copy-val { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.85rem; font-weight: 600; color: var(--text); word-break: break-all; }
    .confirm-irreversible-box {
      padding: 12px 14px;
      border-radius: 12px;
      background: var(--err-soft);
      border: 1px solid var(--err-border);
      color: var(--err);
      font-size: 0.84rem;
      font-weight: 650;
      line-height: 1.5;
      margin-bottom: 12px;
    }

    .muted { color: var(--muted); }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

    /* Responsive Breakpoints */
    @media (max-width: 1120px) {
      .seats-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .overview-grid, .ledger-hero, .diag-kv-grid, .seat-detail-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 920px) {
      .app-shell { grid-template-columns: 1fr; }
      .sidebar {
        position: static;
        height: auto;
        border-right: none;
        border-bottom: 1px solid var(--border);
        padding: 14px 16px;
      }
      .brand { padding-bottom: 10px; margin-bottom: 10px; }
      .side-group { margin-bottom: 8px; }
      .side-group-label { display: none; }
      .side-nav-list { flex-direction: row; flex-wrap: wrap; gap: 6px; }
      .side-nav-btn { width: auto; padding: 7px 11px; }
      .sidebar-foot { display: none; }
      .topbar { padding: 12px 18px; }
      .page-container { padding: 16px 18px 36px; }
      .check-strip, .charts-grid, .create-grid-3, .dist-form-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 600px) {
      .seats-grid, .overview-grid, .ledger-hero, .diag-kv-grid, .seat-detail-kpis, .edit-grid-2 { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <!-- Left Navigation Sidebar -->
    <aside class="sidebar" aria-label="管理总台左侧导航">
      <div class="brand">
        <div class="brand-logo" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
            <path d="M12 2C12.9 7.8 16.2 11.1 22 12C16.2 12.9 12.9 16.2 12 22C11.1 16.2 7.8 12.9 2 12C7.8 11.1 11.1 7.8 12 2Z" fill="currentColor"/>
          </svg>
        </div>
        <div>
          <h1>Gemini Enterprise</h1>
          <div class="brand-sub">本地管理总台 · 默认 Carbon</div>
        </div>
      </div>

      <div class="side-group">
        <div class="side-group-label">运行与测试</div>
        <div class="side-nav-list" role="tablist">
          <button type="button" class="side-nav-btn active" data-nav-page="seats" id="navBtnSeats" role="tab" aria-selected="true">
            <span class="side-nav-left">
              <span class="side-nav-icon" aria-hidden="true">✦</span>
              <span class="side-nav-title">总览与工作席位</span>
            </span>
            <span class="side-badge" id="sideSeatsBadge">待命</span>
          </button>
          <button type="button" class="side-nav-btn" data-nav-page="probe" id="navBtnProbe" role="tab" aria-selected="false">
            <span class="side-nav-left">
              <span class="side-nav-icon" aria-hidden="true">⚡</span>
              <span class="side-nav-title">连通体检与测试</span>
            </span>
            <span class="side-badge" id="sideProbeBadge">Carbon</span>
          </button>
        </div>
      </div>

      <div class="side-group">
        <div class="side-group-label">对外共享与管控</div>
        <div class="side-nav-list" role="tablist">
          <button type="button" class="side-nav-btn" data-nav-page="dist" id="navBtnDist" role="tab" aria-selected="false">
            <span class="side-nav-left">
              <span class="side-nav-icon" aria-hidden="true">🌐</span>
              <span class="side-nav-title">公网通道与密钥</span>
            </span>
            <span class="side-badge" id="navTunnelChip">未开启</span>
          </button>
          <button type="button" class="side-nav-btn" data-nav-page="audit" id="navBtnAudit" role="tab" aria-selected="false">
            <span class="side-nav-left">
              <span class="side-nav-icon" aria-hidden="true">🗂</span>
              <span class="side-nav-title">会话与调用记录</span>
            </span>
            <span class="side-badge" id="navLiveTasksChip">0 会话</span>
          </button>
        </div>
      </div>

      <div class="side-group">
        <div class="side-group-label">统计与系统</div>
        <div class="side-nav-list" role="tablist">
          <button type="button" class="side-nav-btn" data-nav-page="ledger" id="navBtnLedger" role="tab" aria-selected="false">
            <span class="side-nav-left">
              <span class="side-nav-icon" aria-hidden="true">¥</span>
              <span class="side-nav-title">用量与费用账本</span>
            </span>
            <span class="side-badge" id="sideLedgerBadge">¥0.00</span>
          </button>
          <button type="button" class="side-nav-btn" data-nav-page="diag" id="navBtnDiag" role="tab" aria-selected="false">
            <span class="side-nav-left">
              <span class="side-nav-icon" aria-hidden="true">⚙</span>
              <span class="side-nav-title">系统诊断与日志</span>
            </span>
            <span class="side-badge" id="diagStatsChip">0 次</span>
          </button>
        </div>
      </div>

      <div class="sidebar-foot">
        <div class="sidebar-host-card">
          <div class="sidebar-host-top">
            <span>本机免密接口</span>
            <span class="chip ok" style="height:19px;font-size:0.69rem;padding:0 7px;">已保护</span>
          </div>
          <div class="sidebar-host-sub mono" id="sidebarBaseUrlText">http://127.0.0.1:8000/v1</div>
        </div>
        <button type="button" class="btn soft" id="openGuideBtn" style="width:100%;">📋 第三方软件接入指南</button>
      </div>
    </aside>

    <!-- Right Main Stage -->
    <div class="main-stage">
      <header class="topbar">
        <div class="page-heading">
          <h2 id="currentPageTitle">总览与工作席位</h2>
          <p id="currentPageDesc">实时查看服务连接健康度、系统运行概览与并行浏览器工作页记忆余量</p>
        </div>
        <div class="topbar-actions">
          <div class="live-pill" id="connBadge">
            <span class="dot" id="connDot"></span>
            <span id="connText">正在同步状态...</span>
          </div>
          <button type="button" class="btn" id="topGuideBtn">接入指南</button>
          <button type="button" class="btn btn-refresh" id="refreshBtn" title="立即同步最新状态">
            <svg class="refresh-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M20 11A8 8 0 0 0 6.34 6.34L4 8.68M4 4.5V8.7H8.2M4 13A8 8 0 0 0 17.66 17.66L20 15.32M20 19.5V15.3H15.8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <span id="refreshBtnText">刷新</span>
          </button>
        </div>
      </header>

      <main class="page-container">
        <!-- PAGE 1: Overview & Parallel Seats -->
        <section class="page-panel" data-sec="seats" id="secSeats">
          <div class="check-strip" aria-label="连接三步自检">
            <div class="step-card ok" id="stepServiceCard">
              <div class="step-left">
                <span class="step-badge" id="stepServiceIcon">✓</span>
                <div class="step-text">
                  <div class="step-title">本地服务</div>
                  <div class="step-desc" id="stepServiceDesc">8000 · Carbon</div>
                </div>
              </div>
              <div class="step-right">
                <span class="chip ok" id="stepServiceTag">在线</span>
              </div>
            </div>

            <div class="step-card" id="stepExtCard">
              <div class="step-left">
                <span class="step-badge" id="stepExtIcon">2</span>
                <div class="step-text">
                  <div class="step-title">浏览器插件</div>
                  <div class="step-desc" id="stepExtDesc">检测中</div>
                </div>
              </div>
              <div class="step-right">
                <span class="chip" id="stepExtTag">待连接</span>
              </div>
            </div>

            <div class="step-card" id="stepAuthCard">
              <div class="step-left">
                <span class="step-badge" id="stepAuthIcon">3</span>
                <div class="step-text">
                  <div class="step-title">工作页面</div>
                  <div class="step-desc" id="stepAuthDesc">检测中</div>
                </div>
              </div>
              <div class="step-right">
                <a href="https://vertexaisearch.cloud.google.com/" target="_blank" rel="noopener" class="btn sm soft" id="stepAuthBtn" hidden>去打开 ↗</a>
                <span class="chip" id="stepAuthTag">待检测</span>
              </div>
            </div>
          </div>

          <!-- Quick Cross-Page Overview Cards -->
          <div class="overview-grid">
            <button type="button" class="ov-card" data-jump-sec="secProbe">
              <div class="ov-top">
                <span>默认模型与体检</span>
                <span class="chip ok">一键测试 →</span>
              </div>
              <div class="ov-val" id="ovModelVal">Carbon 主力</div>
              <div class="ov-sub" id="ovModelSub">本机免密直连 · 点击快速验证回复</div>
            </button>
            <button type="button" class="ov-card" data-jump-sec="secDist">
              <div class="ov-top">
                <span>公网通道与密钥</span>
                <span class="chip" id="ovTunnelBadge">查看 →</span>
              </div>
              <div class="ov-val" id="ovTunnelVal">未开启公网</div>
              <div class="ov-sub" id="ovTunnelSub">点击管理公网地址与外部专属密钥</div>
            </button>
            <button type="button" class="ov-card" data-jump-sec="secAudit">
              <div class="ov-top">
                <span>会话与在途任务</span>
                <span class="chip" id="ovAuditBadge">管理 →</span>
              </div>
              <div class="ov-val" id="ovAuditVal">0 个会话</div>
              <div class="ov-sub" id="ovAuditSub">查看实时任务、历史会话与拦截记录</div>
            </button>
            <button type="button" class="ov-card" data-jump-sec="secLedger">
              <div class="ov-top">
                <span>今日用量与折算花费</span>
                <span class="chip purple">账本 →</span>
              </div>
              <div class="ov-val" id="ovLedgerVal">¥0.00</div>
              <div class="ov-sub" id="ovLedgerSub">今日 0 次调用 · 点击查看完整账本</div>
            </button>
          </div>

          <!-- Parallel Worker Seats Card -->
          <div class="surface-card">
            <div class="card-header">
              <div class="card-title-group">
                <span class="sec-icon">✦</span>
                <div>
                  <h3 class="card-title">并行工作席位池</h3>
                  <div class="card-subtitle">默认仅展示正在工作或有对话记忆的活跃席位 · 点击任意卡片查看详情或开启新对话</div>
                </div>
              </div>
              <div class="seat-cap-toolbar">
                <div class="seat-cap-group" id="dashSeatCapGroup" role="group" aria-label="席位上限">
                  <span class="seat-cap-label" id="dashSeatCapLabel">席位上限</span>
                  <button type="button" class="seat-cap-btn" data-cap="1">1</button>
                  <button type="button" class="seat-cap-btn" data-cap="5">5</button>
                  <button type="button" class="seat-cap-btn" data-cap="10">10</button>
                  <button type="button" class="seat-cap-btn" id="dashSeatCapCustomBtn" data-cap="custom">自定义</button>
                  <span class="seat-cap-custom-wrap" id="dashSeatCapCustomWrap" hidden>
                    <input type="number" class="seat-cap-input" id="dashSeatCapCustomInput" min="1" max="200" step="1" inputmode="numeric" placeholder="1-200" aria-label="自定义席位数量" />
                    <button type="button" class="seat-cap-apply-btn" id="dashSeatCapCustomApply">确定</button>
                  </span>
                </div>
                <span class="chip" id="seatsSummaryChip">0 个席位待命</span>
              </div>
            </div>

            <div class="seat-detail-panel" id="seatDetailPanel">
              <div class="seat-detail-empty" id="seatDetailEmpty">
                <div class="seat-detail-empty-left">
                  <span class="seat-detail-hint-dot" aria-hidden="true">i</span>
                  <span>点击下方任意席位卡片，可查看记忆容量进度、最近一轮收发字数、压缩次数或单独重置对话</span>
                </div>
              </div>
              <div class="seat-detail-content" id="seatDetailContent" hidden>
                <div class="seat-detail-head">
                  <div class="seat-detail-title-group">
                    <span class="seat-detail-badge" id="detailSeatBadge">1 号席位</span>
                    <span class="chip warn" id="detailTenantChip" hidden>外部共享</span>
                    <span class="seat-detail-title" id="detailSeatTitle">暂无活跃会话</span>
                    <span class="chip" id="detailSourceChip">待命页</span>
                    <span class="chip" id="detailStatusChip">空闲待命</span>
                  </div>
                  <div class="seat-detail-actions">
                    <button type="button" class="btn sm soft" id="detailResetBtn">开启新对话</button>
                    <button type="button" class="btn sm" id="detailCloseBtn">收起详情</button>
                  </div>
                </div>
                <div>
                  <div class="ctx-row">
                    <strong id="detailCtxLeft">记忆池占用：已用 0 / 自动整理线 300.0K (0.0%)</strong>
                    <span class="muted" id="detailCtxRight">距自动整理还剩 300.0K · 总容量上限 1.05M</span>
                  </div>
                  <div class="ctx-track" title="进度条满格为本地自动整理记忆线 (300.0K)，总上下文容量 1,048,576">
                    <div id="detailCtxFill" class="ctx-fill"></div>
                  </div>
                </div>
                <div class="seat-detail-kpis">
                  <div class="detail-kpi">
                    <div class="k">最近一轮收发</div>
                    <div class="v" id="detailTurnIo">暂无记录</div>
                    <div class="s" id="detailStrategy">等待首轮调用</div>
                  </div>
                  <div class="detail-kpi">
                    <div class="k">会话轮次与步数</div>
                    <div class="v" id="detailTurns">0 轮提问 · 0 步交互</div>
                    <div class="s" id="detailReqs">本页累计完成 0 次请求</div>
                  </div>
                  <div class="detail-kpi">
                    <div class="k">上下文压缩</div>
                    <div class="v" id="detailCompactions">0 次（未压缩）</div>
                    <div class="s" id="detailCompactionSub">满 300.0K 自动浓缩换新页</div>
                  </div>
                  <div class="detail-kpi">
                    <div class="k">绑定会话与工作页</div>
                    <div class="v" id="detailSessionId">未绑定固定会话</div>
                    <div class="s" id="detailUpdatedAt">最近活跃 -</div>
                  </div>
                </div>
              </div>
            </div>

            <div class="seats-grid" id="seatsGrid">
              <div class="empty-seats">正在读取工作席位状态...</div>
            </div>

            <div class="idle-fold-wrap" id="idleSeatsWrap" hidden>
              <button type="button" class="idle-fold-btn" id="idleSeatsToggle" aria-expanded="false">
                <span id="idleSeatsLabel">空闲待命席位（0 个）</span>
                <span class="idle-fold-hint" id="idleSeatsHint">展开查看 ▾</span>
              </button>
              <div class="idle-fold-body" id="idleSeatsBody" hidden>
                <div class="seats-grid" id="idleSeatsGrid"></div>
              </div>
            </div>
          </div>
        </section>

        <!-- PAGE 2: Public Gateway & Client API Keys (Clean Two-Card Design) -->
        <section class="page-panel" data-sec="dist" id="secDist" hidden>
          <!-- Card A: Clean Public Tunnel Gateway -->
          <div class="surface-card" id="tunnelBanner">
            <div class="gateway-hero">
              <div class="gateway-top">
                <div class="card-title-group">
                  <span class="sec-icon teal">🌐</span>
                  <div>
                    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                      <h3 class="card-title">Cloudflare 免费公网加密通道</h3>
                      <span class="chip" id="tunnelStatusChip">检测中</span>
                    </div>
                    <div class="card-subtitle" id="tunnelSubHint">免买服务器免域名，一键生成 HTTPS 外部接口地址</div>
                  </div>
                </div>
                <div class="dist-row-actions">
                  <button type="button" class="btn primary" id="tunnelStartQuickBtn">🌐 一键开启免费公网</button>
                  <button type="button" class="btn soft" id="tunnelProbeBtn">🛡 安全自检</button>
                  <button type="button" class="btn" id="tunnelRestartQuickBtn" hidden>换新地址</button>
                  <button type="button" class="btn danger-soft" id="tunnelStopBtn" hidden>关闭公网</button>
                </div>
              </div>

              <div class="gateway-endpoint-bar">
                <div class="gateway-endpoint-meta">
                  <div class="gateway-endpoint-label">对外公网接口地址 (Base URL · 发给外部软件填写)</div>
                  <div class="tunnel-url-val" id="tunnelUrlVal">尚未开启公网通道（点击右上角「一键开启免费公网」自动生成）</div>
                </div>
                <div class="dist-row-actions">
                  <button type="button" class="btn soft" id="tunnelCopyUrlBtn" disabled>复制接口地址</button>
                  <button type="button" class="btn soft" id="tunnelOpenStatusBtn" title="打开对外公开的实时可用性与延迟监控面板">📊 公开监控页</button>
                  <button type="button" class="btn soft" id="tunnelCopyStatusBtn" disabled title="复制公开状态监控网址发给外部用户">复制监控链接</button>
                  <button type="button" class="btn soft" id="tunnelOpenMapBtn" disabled title="打开内置 Globalping 全国三网与海外真实物理节点测速地图">🗺️ 全国延迟地图</button>
                </div>
              </div>

              <div class="tunnel-probe-box" id="tunnelProbeBox" hidden></div>

              <div class="gateway-foot-note">
                <span id="distPoolSplitText">本机直连免密且优先使用席位；管理页面对外完全隐藏</span>
                <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                  <span class="chip ok" id="distAccessBadge">外部共享已开启</span>
                  <span class="chip" id="distSummaryChip">本机预留 2 席 · 0 把密钥</span>
                </div>
              </div>
            </div>

            <!-- Tucked-away Advanced Routing & Custom Domain Settings -->
            <details class="settings-drawer" id="gatewaySettingsDrawer" style="margin-top:14px;">
              <summary>
                <span>⚙ 进阶设置：主机保底席位策略 / 绑定自有固定域名</span>
                <span class="drawer-chevron"><span class="drawer-text-closed">展开设置</span><span class="drawer-text-open">收起设置</span><span class="drawer-arrow">▾</span></span>
              </summary>
              <div class="settings-drawer-body">
                <div>
                  <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;flex-wrap:wrap;">
                    <strong style="font-size:0.86rem;">主机保底与公网排队规则</strong>
                    <div style="display:flex;align-items:center;gap:8px;">
                      <span class="muted" style="font-size:0.78rem;" id="distPolicyMsg"></span>
                      <button type="button" class="btn sm primary" id="distSavePolicyBtn">保存分流设置</button>
                    </div>
                  </div>
                  <div class="dist-form-grid">
                    <div class="dist-field">
                      <label for="distEnabledSelect">外部接入总开关</label>
                      <select id="distEnabledSelect" class="clean-select">
                        <option value="1">允许接入（凭专属密钥）</option>
                        <option value="0">暂停全部外部接入（仅本机可用）</option>
                      </select>
                    </div>
                    <div class="dist-field">
                      <label for="distReservedInput">为本机保底预留席位数</label>
                      <input id="distReservedInput" class="clean-input" type="number" min="0" max="20" value="2" />
                    </div>
                    <div class="dist-field">
                      <label for="distQueueTimeoutInput">外部满载排队超时（秒）</label>
                      <input id="distQueueTimeoutInput" class="clean-input" type="number" min="5" max="600" value="90" />
                    </div>
                    <div class="dist-field">
                      <label for="distPublicUrlInput">自定义外部域名（选填，用于生成接入包）</label>
                      <input id="distPublicUrlInput" class="clean-input" type="text" spellcheck="false" placeholder="如 https://ai.example.com" />
                    </div>
                  </div>
                </div>

                <div id="tunnelNamedDrawer" style="border-top:1px dashed var(--border);padding-top:14px;">
                  <div style="font-size:0.86rem;font-weight:700;margin-bottom:4px;">切换为固定域名 Cloudflare Tunnel（可选）</div>
                  <div class="muted" style="font-size:0.78rem;margin-bottom:10px;">如果你在 Cloudflare 绑定了自己的域名，填入后台生成的令牌即可使用固定不变的网址。</div>
                  <div class="dist-form-grid" style="grid-template-columns: 1.5fr 1.2fr auto;">
                    <div class="dist-field">
                      <label for="tunnelNamedTokenInput">Cloudflare Tunnel Token</label>
                      <input id="tunnelNamedTokenInput" class="clean-input" type="password" autocomplete="off" placeholder="粘贴 eyJ... 令牌" />
                    </div>
                    <div class="dist-field">
                      <label for="tunnelNamedUrlInput">绑定的固定公网域名</label>
                      <input id="tunnelNamedUrlInput" class="clean-input" type="text" spellcheck="false" placeholder="如 https://api.yourdomain.com" />
                    </div>
                    <button type="button" class="btn sm primary" id="tunnelStartNamedBtn">启用固定域名</button>
                  </div>
                </div>
              </div>
            </details>
          </div>

          <!-- Card B: Dedicated API Keys Management -->
          <div class="surface-card">
            <div class="card-header">
              <div class="card-title-group">
                <span class="sec-icon indigo">🔑</span>
                <div>
                  <h3 class="card-title">客户端专属密钥管理</h3>
                  <div class="card-subtitle">一人一钥，独立隔离对话记忆、文件与每日配额 · 支持一键复制完整接入包</div>
                </div>
              </div>
              <button type="button" class="btn primary" id="toggleCreateKeyBtn">+ 新建专属密钥</button>
            </div>

            <!-- Collapsible Key Creation Form -->
            <div class="create-key-panel" id="createKeyPanel" hidden>
              <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
                <strong style="font-size:0.92rem;">创建新的客户端专属密钥</strong>
                <span class="muted" style="font-size:0.78rem;">限额填 0 表示不设上限，创建后随时可修改</span>
              </div>
              <div class="create-grid-3">
                <div class="dist-field">
                  <label for="distClientName">使用者名称（必填）</label>
                  <input id="distClientName" class="clean-input" type="text" maxlength="64" placeholder="如：朋友小张 / 副机笔记本" />
                </div>
                <div class="dist-field">
                  <label for="distClientConcurrency">最大同时并发数</label>
                  <input id="distClientConcurrency" class="clean-input" type="number" min="1" max="20" value="2" />
                </div>
                <div class="dist-field">
                  <label for="distClientRpm">每分钟限次 (RPM)</label>
                  <input id="distClientRpm" class="clean-input" type="number" min="1" max="600" value="30" />
                </div>
                <div class="dist-field">
                  <label for="distClientDailyReq">每日调用次数上限（0 不限）</label>
                  <input id="distClientDailyReq" class="clean-input" type="number" min="0" value="0" />
                </div>
                <div class="dist-field">
                  <label for="distClientDailyTok">每日 Token 上限（0 不限）</label>
                  <input id="distClientDailyTok" class="clean-input" type="number" min="0" value="0" />
                </div>
                <div class="dist-field">
                  <label for="distClientNote">备注说明（选填）</label>
                  <input id="distClientNote" class="clean-input" type="text" maxlength="120" placeholder="例如：日常编程使用" />
                </div>
              </div>
              <details style="margin-top:12px;font-size:0.8rem;color:var(--muted);">
                <summary style="cursor:pointer;font-weight:650;">更多可选限制：每日金额预算 / 总字数封顶 / 到期日期 / 限定模型 / IP 白名单 ▾</summary>
                <div class="create-grid-3" style="margin-top:10px;">
                  <div class="dist-field">
                    <label for="distClientDailyCost">每日参考预算上限 ($ 美元)</label>
                    <input id="distClientDailyCost" class="clean-input" type="number" min="0" step="0.1" value="0" />
                  </div>
                  <div class="dist-field">
                    <label for="distClientTotalTok">累计总 Token 封顶</label>
                    <input id="distClientTotalTok" class="clean-input" type="number" min="0" value="0" />
                  </div>
                  <div class="dist-field">
                    <label for="distClientExpiresDate">到期日期（留空永不过期）</label>
                    <input id="distClientExpiresDate" class="clean-input" type="date" />
                  </div>
                  <div class="dist-field">
                    <label for="distClientAllowedModels">允许使用的模型（逗号分隔）</label>
                    <input id="distClientAllowedModels" class="clean-input" type="text" placeholder="留空允许全部模型" />
                  </div>
                  <div class="dist-field" style="grid-column: span 2;">
                    <label for="distClientAllowedIps">允许访问的 IP 白名单（逗号分隔）</label>
                    <input id="distClientAllowedIps" class="clean-input" type="text" placeholder="如 203.0.113.5, 10.0.0.0/8（留空不限网络）" />
                  </div>
                </div>
              </details>
              <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
                <button type="button" class="btn" id="cancelCreateKeyBtn">收起</button>
                <button type="button" class="btn primary" id="distCreateClientBtn">✓ 生成专属密钥</button>
              </div>
            </div>

            <!-- Generated Share Bundle Box -->
            <div class="dist-share-box" id="distShareBox" hidden>
              <div class="dist-share-head">
                <span id="distShareTitle">API 接入凭证 (API Credentials)</span>
                <div class="dist-row-actions">
                  <button type="button" class="btn sm primary" id="distCopyBundleBtn">复制完整接入包</button>
                  <button type="button" class="btn sm soft" id="distCopyKeyOnlyBtn">仅复制密钥</button>
                  <button type="button" class="btn sm" id="distCloseShareBtn">收起</button>
                </div>
              </div>
              <pre class="dist-share-pre" id="distSharePre"></pre>
            </div>

            <div class="table-viewport">
              <table class="clean-table" id="distClientsTable">
                <thead>
                  <tr>
                    <th>使用者与状态</th>
                    <th>专属密钥与接入包</th>
                    <th>并发 · 速率</th>
                    <th>配额使用进度</th>
                    <th>累计用量 · 最近活跃</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody id="distClientsBody">
                  <tr><td colspan="6" class="muted">暂未创建外部密钥 · 如需分享给他人请点击右上角「+ 新建专属密钥」</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>

        <!-- PAGE 3: Sessions & Task Call Audit (Bounded Height + Sub-Tabs + Pagination + Expandable Rows) -->
        <section class="page-panel" data-sec="audit" id="secAudit" hidden>
          <!-- Compact Collapsible Live In-Flight Strip -->
          <details class="settings-drawer" id="liveTasksFold">
            <summary>
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <span>⚡ 实时正在运行与排队中的任务</span>
                <span class="chip" id="liveTasksCountChip">当前空闲</span>
              </div>
              <span class="drawer-chevron"><span class="drawer-text-closed">展开看板</span><span class="drawer-text-open">收起看板</span><span class="drawer-arrow">▾</span></span>
            </summary>
            <div class="settings-drawer-body" style="padding:12px 14px;">
              <div class="table-viewport compact">
                <table class="clean-table" id="liveTasksTable">
                  <thead>
                    <tr>
                      <th>状态</th>
                      <th>调用方 / 来源</th>
                      <th>用途 · 模型</th>
                      <th>工作席位</th>
                      <th>任务提问摘要</th>
                      <th class="num">已耗时</th>
                    </tr>
                  </thead>
                  <tbody id="liveTasksBody">
                    <tr><td colspan="6" class="muted">当前没有正在执行或排队的请求</td></tr>
                  </tbody>
                </table>
              </div>
            </div>
          </details>

          <!-- Bounded Tabbed Card for Sessions vs Audit Logs -->
          <div class="surface-card">
            <div class="sub-tabs-bar">
              <div class="seg-tabs" id="auditSubTabs" role="tablist">
                <button type="button" class="seg-tab active" data-audit-tab="sessions" role="tab" aria-selected="true">
                  <span>💬 会话列表管理</span>
                  <span class="chip" id="sessionsTabBadge" style="height:19px;padding:0 7px;font-size:0.71rem;">0</span>
                </button>
                <button type="button" class="seg-tab" data-audit-tab="logs" role="tab" aria-selected="false">
                  <span>📜 任务调用与拦截记录</span>
                  <span class="chip" id="auditCountChip" style="height:19px;padding:0 7px;font-size:0.71rem;">0</span>
                </button>
              </div>
              <span class="chip" id="auditSummaryChip">0 在途 · 0 会话 · 0 条记录</span>
            </div>

            <!-- Sub-Pane 1: Sessions Management -->
            <div id="auditPaneSessions">
              <div class="audit-toolbar">
                <div class="audit-filters">
                  <select id="sessionTenantFilter" class="clean-select" style="height:32px;font-size:0.81rem;">
                    <option value="all">全部调用方会话</option>
                    <option value="host">本机直连</option>
                  </select>
                  <input id="sessionSearchInput" class="clean-input" type="text" placeholder="搜索会话标题、会话 ID 或模型..." style="flex:1;min-width:200px;max-width:320px;height:32px;font-size:0.81rem;" />
                </div>
                <div class="dist-row-actions">
                  <button type="button" class="btn sm danger-soft" id="sessionPurgeTenantBtn" hidden>清空所选客户端全部会话</button>
                  <select id="sessionPageSizeSelect" class="clean-select" style="height:32px;font-size:0.79rem;" aria-label="会话每页显示条数">
                    <option value="10">每页 10 条</option>
                    <option value="15" selected>每页 15 条</option>
                    <option value="30">每页 30 条</option>
                  </select>
                </div>
              </div>

              <div class="table-viewport">
                <table class="clean-table" id="sessionsTable">
                  <thead>
                    <tr>
                      <th>状态</th>
                      <th>所属调用方</th>
                      <th>会话标题（点击展开详情）</th>
                      <th>模型 · 轮数</th>
                      <th>记忆占用</th>
                      <th>最近活跃</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody id="sessionsTableBody">
                    <tr><td colspan="7" class="muted">暂无活跃或历史归档会话</td></tr>
                  </tbody>
                </table>
              </div>

              <div class="pager-bar" id="sessionsPager">
                <span id="sessionsPageInfo">共 0 条会话</span>
                <div class="pager-controls">
                  <button type="button" class="btn sm" id="sessionsPrevBtn" disabled>上一页</button>
                  <span class="chip" id="sessionsPageBadge">1 / 1</span>
                  <button type="button" class="btn sm" id="sessionsNextBtn" disabled>下一页</button>
                </div>
              </div>
            </div>

            <!-- Sub-Pane 2: Call & Interception Audit Logs -->
            <div id="auditPaneLogs" hidden>
              <div class="audit-toolbar">
                <div class="audit-filters">
                  <select id="auditTenantFilter" class="clean-select" style="height:32px;font-size:0.81rem;">
                    <option value="all">全部调用方（本机 + 外部密钥）</option>
                    <option value="host">本机直连</option>
                  </select>
                  <select id="auditStatusFilter" class="clean-select" style="height:32px;font-size:0.81rem;">
                    <option value="all">全部状态（成功 + 限流 + 拦截 + 异常）</option>
                    <option value="ok">仅看成功调用</option>
                    <option value="blocked">仅看限流与权限拦截 (401/403/429)</option>
                    <option value="error">仅看执行报错</option>
                  </select>
                  <input id="auditSearchInput" class="clean-input" type="text" placeholder="搜索提问内容、回复、拦截原因、IP 或模型..." style="flex:1;min-width:200px;height:32px;font-size:0.81rem;" />
                </div>
                <select id="auditPageSizeSelect" class="clean-select" style="height:32px;font-size:0.79rem;" aria-label="记录每页显示条数">
                  <option value="10">每页 10 条</option>
                  <option value="15" selected>每页 15 条</option>
                  <option value="30">每页 30 条</option>
                </select>
              </div>

              <div class="table-viewport">
                <table class="clean-table" id="auditLogTable">
                  <thead>
                    <tr>
                      <th>时间 · 状态</th>
                      <th>调用方 · 来源</th>
                      <th>用途 · 模型</th>
                      <th>内容摘要（点击展开完整问答/原因）</th>
                      <th>Token · 折算花费</th>
                      <th class="num">耗时</th>
                    </tr>
                  </thead>
                  <tbody id="auditLogBody">
                    <tr><td colspan="6" class="muted">暂无任务调用审计记录</td></tr>
                  </tbody>
                </table>
              </div>

              <div class="pager-bar" id="auditLogsPager">
                <span id="auditLogsPageInfo">共 0 条记录</span>
                <div class="pager-controls">
                  <button type="button" class="btn sm" id="auditLogsPrevBtn" disabled>上一页</button>
                  <span class="chip" id="auditLogsPageBadge">1 / 1</span>
                  <button type="button" class="btn sm" id="auditLogsNextBtn" disabled>下一页</button>
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- PAGE 4: Usage & Reference Cost Ledger -->
        <section class="page-panel" data-sec="ledger" id="secLedger" hidden>
          <div class="surface-card" style="padding:14px 18px;">
            <div class="period-bar" style="background:transparent;border:none;padding:0;">
              <div class="period-tabs" id="periodTabs">
                <button type="button" class="period-tab" data-period="today">今日</button>
                <button type="button" class="period-tab" data-period="yesterday">昨日</button>
                <button type="button" class="period-tab" data-period="7d">近 7 天</button>
                <button type="button" class="period-tab" data-period="30d">近 30 天</button>
                <button type="button" class="period-tab" data-period="session">本次启动</button>
                <button type="button" class="period-tab active" data-period="all">全部累计</button>
              </div>
              <div class="date-picker-wrap">
                <select id="ledgerTenantSelect" class="clean-select" style="height:30px;padding:0 10px;font-size:0.79rem;min-width:170px;" aria-label="按 API 密钥筛选统计">
                  <option value="all">全部汇总（本机 + 所有外部密钥）</option>
                  <option value="host">本机直连</option>
                </select>
                <input type="date" id="datePickerInput" class="date-input" aria-label="按日期查看统计" />
                <select id="dayFilterSelect" hidden></select>
                <span class="chip" id="activePeriodTag">全部累计</span>
                <div class="currency-switch" id="currencySwitch" title="切换显示币种">
                  <button type="button" class="cur-btn active" data-cur="CNY">¥ 人民币</button>
                  <button type="button" class="cur-btn" data-cur="USD">$ 美元</button>
                </div>
              </div>
            </div>
          </div>

          <div class="ledger-hero">
            <div class="stat-tile">
              <div class="stat-head">
                <span id="cardActualLabel">预估等效花费</span>
                <div class="stat-icon-box emerald" aria-hidden="true">¥</div>
              </div>
              <div class="stat-big" id="ledgerActualMain">¥0.00</div>
              <div class="stat-sub" id="ledgerActualSub">输入 / 缓存 / 输出单价折算</div>
            </div>

            <div class="stat-tile">
              <div class="stat-head">
                <span id="cardCallsLabel">调用总次数</span>
                <div class="stat-icon-box blue" aria-hidden="true">⚡</div>
              </div>
              <div class="stat-big" id="ledgerCallsMain">0 次</div>
              <div class="stat-sub" id="ledgerCallsSub">编程 0 / 对话 0 / 整理 0</div>
            </div>

            <div class="stat-tile">
              <div class="stat-head">
                <span id="cardCacheLabel">缓存命中率</span>
                <div class="stat-icon-box teal" aria-hidden="true">◈</div>
              </div>
              <div class="stat-big" id="ledgerCacheMain">0.0%</div>
              <div class="stat-sub" id="ledgerCacheSub">缓存 0 / 总输入 0</div>
            </div>

            <div class="stat-tile">
              <div class="stat-head">
                <span id="cardTokensLabel">累计 Token</span>
                <div class="stat-icon-box indigo" aria-hidden="true">∑</div>
              </div>
              <div class="stat-big" id="ledgerTokensMain">0</div>
              <div class="stat-sub" id="ledgerTokensSub">输入 0 / 输出 0 / 缓存 0</div>
            </div>
          </div>

          <div class="charts-grid">
            <div class="chart-box">
              <div class="chart-title-row">
                <span>每日花费趋势（点击柱子可筛选当天）</span>
                <div class="chart-legend">
                  <span><i class="leg-dot" style="background:#2563eb;"></i>等效折算花费（非 Google 实际扣费）</span>
                </div>
              </div>
              <div class="bars-stage" id="dailyBarsStage"></div>
            </div>

            <div class="chart-box">
              <div class="chart-title-row">
                <span>用途分布占比</span>
                <span class="muted" style="font-size:0.75rem;" id="ledgerRateBadge">Carbon 参考价</span>
              </div>
              <div class="task-ratio-bar" id="taskRatioBar"></div>
              <div class="task-list" id="taskBreakdownList"></div>
            </div>
          </div>

          <!-- Bounded Tabbed Detail Tables -->
          <div class="surface-card">
            <div class="sub-tabs-bar">
              <div class="seg-tabs" id="ledgerSubTabs" role="tablist">
                <button type="button" class="seg-tab active" data-ledger-tab="tenants" role="tab">🔑 各密钥对比排行</button>
                <button type="button" class="seg-tab" data-ledger-tab="models" role="tab">🤖 按模型汇总</button>
                <button type="button" class="seg-tab" data-ledger-tab="daily" role="tab">📅 按日期汇总</button>
                <button type="button" class="seg-tab" data-ledger-tab="recent" role="tab">🧾 最近调用流水</button>
              </div>
              <button type="button" class="btn sm soft" id="ledgerResetTenantFilterBtn" hidden>返回全部汇总</button>
            </div>

            <div id="ledgerPaneTenants">
              <div class="table-viewport">
                <table class="clean-table" id="ledgerTenantTable">
                  <thead>
                    <tr>
                      <th>调用方 / API 密钥</th>
                      <th class="num">成功调用</th>
                      <th class="num">拦截/报错</th>
                      <th class="num">累计 Token</th>
                      <th>输入 / 输出 / 缓存</th>
                      <th class="num">等效花费</th>
                      <th>单独筛选</th>
                    </tr>
                  </thead>
                  <tbody id="ledgerTenantBody">
                    <tr><td colspan="7" class="muted">暂无分密钥用量记录</td></tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div id="ledgerPaneModels" hidden>
              <div class="table-viewport">
                <table class="clean-table">
                  <thead>
                    <tr>
                      <th>模型名称</th>
                      <th class="num">调用次数</th>
                      <th class="num">累计 Token</th>
                      <th>输入 / 输出 / 缓存</th>
                      <th>专项生成</th>
                      <th class="num">等效花费</th>
                    </tr>
                  </thead>
                  <tbody id="ledgerModelBody">
                    <tr><td colspan="6" class="muted">暂无记录</td></tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div id="ledgerPaneDaily" hidden>
              <div class="table-viewport">
                <table class="clean-table">
                  <thead>
                    <tr>
                      <th>日期</th>
                      <th class="num">调用次数</th>
                      <th class="num">累计 Token</th>
                      <th>输入 / 输出 / 缓存</th>
                      <th>专项统计</th>
                      <th class="num">等效花费</th>
                    </tr>
                  </thead>
                  <tbody id="ledgerDailyBody">
                    <tr><td colspan="6" class="muted">暂无按日记录</td></tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div id="ledgerPaneRecent" hidden>
              <div class="table-viewport">
                <table class="clean-table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>用途</th>
                      <th>模型 · 工作席位</th>
                      <th>对话方式</th>
                      <th>总计（输入 / 输出 / 缓存）</th>
                      <th class="num">等效花费</th>
                      <th class="num">耗时</th>
                    </tr>
                  </thead>
                  <tbody id="ledgerRecentBody">
                    <tr><td colspan="7" class="muted">暂无最近流水</td></tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </section>

        <!-- PAGE 5: Connectivity Health Probe & Multimodal Playground -->
        <section class="page-panel" data-sec="probe" id="secProbe" hidden>
          <div class="surface-card">
            <div class="card-header">
              <div class="card-title-group">
                <span class="sec-icon green">⚡</span>
                <div>
                  <h3 class="card-title">一键模型连通体检</h3>
                  <div class="card-subtitle">无需手动输入，点击即可验证网页端真实回复与响应耗时</div>
                </div>
              </div>
              <span class="chip" id="probeHeaderChip">默认测 Carbon</span>
            </div>

            <div class="probe-bar">
              <div class="probe-controls">
                <select id="modelSelect" class="clean-select" aria-label="选择测试模型">
                  <option value="gemini-4-carbon" selected>Carbon（默认主力）</option>
                  <option value="gemini-4-hydrogen">Hydrogen（深度推理）</option>
                  <option value="gemini-4-argon">Argon（高阶推理）</option>
                  <option value="gemini-3.1-pro">Gemini 3.1 Pro</option>
                  <option value="gemini-3.8-flash">Gemini 3.8 Flash</option>
                </select>
                <button type="button" class="btn primary" id="quickProbeBtn">⚡ 一键快速体检</button>
              </div>
              <span class="muted" style="font-size:0.81rem;" id="probeHint">自动发送简短口令并核验真实流式回复</span>
            </div>

            <div class="probe-result" id="probeResultBox" hidden>
              <div class="probe-res-head">
                <span id="probeResTitle">测试结果</span>
                <span class="chip" id="probeResMeta">-</span>
              </div>
              <pre class="probe-res-body" id="replyOut">等待测试输出...</pre>
            </div>
          </div>

          <div class="surface-card" id="customTestDrawer">
            <div class="card-header">
              <div class="card-title-group">
                <span class="sec-icon">💬</span>
                <div>
                  <h3 class="card-title">自定义提问与多模态文件测试</h3>
                  <div class="card-subtitle">支持直接上传图片、PDF、Word、表格、音频或视频验证内容解析与回复</div>
                </div>
              </div>
            </div>
            <div style="display:flex;flex-direction:column;gap:12px;">
              <textarea id="promptInput" class="clean-textarea" rows="4" placeholder="输入你想测试的问题，也可在下方选择附件一起发送..."></textarea>
              <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
                <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
                  <input id="fileInput" type="file" multiple style="font-size:0.82rem;color:var(--muted);" />
                  <details style="font-size:0.79rem;color:var(--muted);">
                    <summary style="cursor:pointer;">高级：填写指定访问密钥测试（本机默认留空）</summary>
                    <input id="apiKeyInput" class="clean-input" type="password" autocomplete="off" spellcheck="false" placeholder="默认留空免密" style="margin-top:6px;width:250px;" />
                  </details>
                </div>
                <button type="button" class="btn soft" id="sendBtn">发送自定义提问</button>
              </div>
            </div>
          </div>
        </section>

        <!-- PAGE 6: Diagnostics & Gateway Logs -->
        <section class="page-panel" data-sec="diag" id="secDiag" hidden>
          <div class="diag-kv-grid">
            <div class="diag-kv"><div class="k">运行与记忆模式</div><div class="v" id="modeVal">-</div></div>
            <div class="diag-kv"><div class="k">浏览器连接通道</div><div class="v" id="transportVal">-</div></div>
            <div class="diag-kv"><div class="k">网页流监听状态</div><div class="v" id="rpcVal">-</div></div>
            <div class="diag-kv"><div class="k">并发 / 排队 / 累计请求</div><div class="v" id="statsVal">0 / 0 / 0</div></div>
          </div>

          <div class="surface-card">
            <div class="card-header">
              <div class="card-title-group">
                <span class="sec-icon slate">⚙</span>
                <div>
                  <h3 class="card-title">网关底层实时运行日志</h3>
                  <div class="card-subtitle">按时间倒序展示最近状态变更、流式响应与通道事件（固定高度窗口滚动）</div>
                </div>
              </div>
            </div>
            <div class="logs-box" id="logsBox">暂无运行记录</div>
          </div>
        </section>
      </main>
    </div>
  </div>

  <!-- Integration Guide Modal -->
  <div class="modal-backdrop" id="guideModal" hidden>
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="guideModalTitle">
      <div class="modal-head">
        <h3 id="guideModalTitle">第三方软件接入指南（一键复制）</h3>
        <button type="button" class="btn sm" id="closeGuideBtn">关闭</button>
      </div>
      <p class="muted" style="margin:0 0 14px;font-size:0.84rem;">
        支持 GitHub Copilot、Cursor、Cline、Cherry Studio、ChatBox 等所有兼容 OpenAI 接口的软件接入本机服务：
      </p>
      <div class="copy-row">
        <div class="copy-meta">
          <div class="copy-label">接口地址 (Base URL / API Host)</div>
          <div class="copy-val" id="baseUrlVal">http://127.0.0.1:8000/v1</div>
        </div>
        <button type="button" class="btn sm soft" data-copy-target="baseUrlVal">复制</button>
      </div>
      <div class="copy-row">
        <div class="copy-meta">
          <div class="copy-label">默认主力模型（推荐填写）</div>
          <div class="copy-val" id="copyCarbonVal">gemini-4-carbon</div>
        </div>
        <button type="button" class="btn sm soft" data-copy-target="copyCarbonVal">复制</button>
      </div>
      <div class="copy-row">
        <div class="copy-meta">
          <div class="copy-label">其他可选模型</div>
          <div class="copy-val" id="copyOtherModels">gemini-4-hydrogen, gemini-4-argon, gemini-3.1-pro, gemini-3.8-flash</div>
        </div>
        <button type="button" class="btn sm soft" data-copy-target="copyOtherModels">复制</button>
      </div>
      <div class="copy-row" style="margin-bottom:0;">
        <div class="copy-meta">
          <div class="copy-label">API Key（访问密钥）</div>
          <div class="copy-val">本机直连免密，留空或任意填写均可</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Edit Client Quota & Restrictions Modal -->
  <div class="modal-backdrop" id="clientEditModal" hidden>
    <div class="modal-card wide" role="dialog" aria-modal="true" aria-labelledby="clientEditModalTitle">
      <div class="modal-head">
        <h3 id="clientEditModalTitle">编辑客户端密钥配额与访问限制</h3>
        <button type="button" class="btn sm" id="closeClientEditBtn">关闭</button>
      </div>
      <input type="hidden" id="editClientId" />
      <div class="edit-grid-2">
        <div class="dist-field">
          <label for="editClientName">客户端 / 使用者名称</label>
          <input id="editClientName" class="clean-input" type="text" maxlength="64" />
        </div>
        <div class="dist-field">
          <label for="editClientNote">备注说明</label>
          <input id="editClientNote" class="clean-input" type="text" maxlength="120" />
        </div>
        <div class="dist-field">
          <label for="editClientConcurrency">单客最大同时并发数</label>
          <input id="editClientConcurrency" class="clean-input" type="number" min="1" max="20" />
        </div>
        <div class="dist-field">
          <label for="editClientRpm">每分钟请求次数上限 (RPM)</label>
          <input id="editClientRpm" class="clean-input" type="number" min="1" max="600" />
        </div>
        <div class="dist-field">
          <label for="editClientDailyReq">每日调用次数上限（0 为不限）</label>
          <input id="editClientDailyReq" class="clean-input" type="number" min="0" />
        </div>
        <div class="dist-field">
          <label for="editClientDailyTok">每日 Token 上限（0 为不限）</label>
          <input id="editClientDailyTok" class="clean-input" type="number" min="0" />
        </div>
        <div class="dist-field">
          <label for="editClientDailyCost">每日参考预算上限 $ USD（0 为不限）</label>
          <input id="editClientDailyCost" class="clean-input" type="number" min="0" step="0.1" />
        </div>
        <div class="dist-field">
          <label for="editClientTotalTok">累计总 Token 封顶（0 为不限）</label>
          <input id="editClientTotalTok" class="clean-input" type="number" min="0" />
        </div>
        <div class="dist-field">
          <label for="editClientExpiresDate">密钥到期日（清空表示永不过期）</label>
          <input id="editClientExpiresDate" class="clean-input" type="date" />
        </div>
        <div class="dist-field">
          <label for="editClientAllowedModels">允许调用的模型（逗号分隔，留空允许全部）</label>
          <input id="editClientAllowedModels" class="clean-input" type="text" placeholder="如 gemini-4-carbon, gemini-4-argon" />
        </div>
      </div>
      <div class="dist-field" style="margin-bottom:14px;">
        <label for="editClientAllowedIps">允许访问的来源 IP 或网段白名单（逗号分隔，留空允许任意网络访问）</label>
        <input id="editClientAllowedIps" class="clean-input" type="text" placeholder="如 203.0.113.8, 198.51.100.0/24" />
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <span class="muted" style="font-size:0.8rem;" id="editClientMsg">保存后立即对该密钥的新请求生效</span>
        <div class="dist-row-actions">
          <button type="button" class="btn sm" id="editResetTodayQuotaBtn">清零今日已用额度</button>
          <button type="button" class="btn primary" id="saveClientEditBtn">保存修改</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Irreversible Tunnel Action Confirmation Modal -->
  <div class="modal-backdrop" id="tunnelConfirmModal" hidden>
    <div class="modal-card" style="max-width:480px;" role="dialog" aria-modal="true" aria-labelledby="tunnelConfirmTitle">
      <div class="modal-head">
        <h3 id="tunnelConfirmTitle">确认执行公网通道操作？</h3>
        <button type="button" class="btn sm" id="closeTunnelConfirmBtn">取消</button>
      </div>
      <div class="confirm-irreversible-box" id="tunnelConfirmWarnBox">
        ⚠ 此操作不可逆，执行后无法恢复原状！
      </div>
      <div class="copy-row" id="tunnelConfirmUrlRow" style="margin-bottom:12px;">
        <div class="copy-meta">
          <div class="copy-label" id="tunnelConfirmUrlLabel">当前公网接口地址（操作后将失效）</div>
          <div class="copy-val" id="tunnelConfirmUrlVal">-</div>
        </div>
      </div>
      <p class="muted" id="tunnelConfirmDesc" style="margin:0 0 18px;font-size:0.83rem;line-height:1.55;"></p>
      <div style="display:flex;align-items:center;justify-content:flex-end;gap:10px;">
        <button type="button" class="btn" id="cancelTunnelConfirmBtn">我再想想</button>
        <button type="button" class="btn danger" id="confirmTunnelActionBtn">确认执行</button>
      </div>
    </div>
  </div>
  <script>
    (function () {
      const baseOrigin = window.location.origin;
      const apiV1 = baseOrigin + '/v1';
      document.getElementById('baseUrlVal').textContent = apiV1;
      const sideBaseEl = document.getElementById('sidebarBaseUrlText');
      if (sideBaseEl) sideBaseEl.textContent = apiV1;

      const PAGE_META = {
        seats: {
          secId: 'secSeats',
          title: '总览与工作席位',
          desc: '实时查看服务连接健康度、系统运行概览与并行浏览器工作页记忆余量'
        },
        probe: {
          secId: 'secProbe',
          title: '连通体检与测试',
          desc: '一键快速测试模型真实回复速度，或上传图片、文档与音视频进行多模态验证'
        },
        dist: {
          secId: 'secDist',
          title: '公网通道与密钥',
          desc: '一键开启免费 HTTPS 公网通道，并为外部使用者分发独立配额与记忆隔离的专属密钥'
        },
        audit: {
          secId: 'secAudit',
          title: '会话与调用记录',
          desc: '分页浏览与管理各调用方的活跃或历史会话，按需展开查看完整问答摘要与拦截原因'
        },
        ledger: {
          secId: 'secLedger',
          title: '用量与费用账本',
          desc: '基于本地字数估算与官方参考单价折算的用量统计（非 Google 网页实际扣费）'
        },
        diag: {
          secId: 'secDiag',
          title: '系统诊断与日志',
          desc: '查看浏览器桥接通道状态、流式监听模式与实时底层网关运行日志'
        }
      };

      const SEC_TO_PAGE = {
        secSeats: 'seats',
        secProbe: 'probe',
        secDist: 'dist',
        secAudit: 'audit',
        secLedger: 'ledger',
        secDiag: 'diag'
      };

      let activePage = 'seats';
      let activeAuditTab = 'sessions';
      let activeLedgerTab = 'tenants';

      let selectedPeriod = 'all';
      let selectedTenant = 'all';
      let selectedCurrency = 'CNY';
      try {
        const savedCur = localStorage.getItem('geb_currency');
        if (savedCur === 'USD' || savedCur === 'CNY') selectedCurrency = savedCur;
      } catch (_) {}

      let sessionsPage = 1;
      let sessionsPageSize = 15;
      let auditLogsPage = 1;
      let auditLogsPageSize = 15;
      const expandedSessionKeys = new Set();
      const expandedAuditKeys = new Set();
      let openClientMenuId = '';
      let createKeyPanelOpen = false;
      let createKeyAutoInitialized = false;
      const pendingConfirmMap = {};

      let lastUsageSnapshot = null;
      let lastCtxSnapshot = null;
      let seatIndexByTabId = {};
      let seatIndexBySlotKey = {};
      let selectedSlotKey = null;
      let idleSeatsExpanded = false;
      let enrichedBySlotKey = {};
      let refreshResetTimer = null;
      let lastDistSnapshot = null;
      let lastTunnelSnapshot = null;
      let tunnelActionInFlight = false;
      let lastSessionsOverview = [];
      let lastDefaultModel = 'gemini-4-carbon';
      let distPolicyDirty = false;
      let currentShareKey = '';
      let currentShareBundle = '';
      let editingClientId = '';

      function switchPage(pageOrSec, updateHash) {
        const key = PAGE_META[pageOrSec] ? pageOrSec : (SEC_TO_PAGE[pageOrSec] || 'seats');
        activePage = key;
        const meta = PAGE_META[key];
        document.getElementById('currentPageTitle').textContent = meta.title;
        document.getElementById('currentPageDesc').textContent = meta.desc;
        document.querySelectorAll('.page-panel').forEach(function (sec) {
          sec.hidden = (sec.id !== meta.secId);
        });
        document.querySelectorAll('.side-nav-btn[data-nav-page]').forEach(function (btn) {
          const isAct = btn.getAttribute('data-nav-page') === key;
          btn.classList.toggle('active', isAct);
          btn.setAttribute('aria-selected', isAct ? 'true' : 'false');
        });
        try { localStorage.setItem('geb_active_page', key); } catch (_) {}
        if (updateHash !== false) {
          try { history.replaceState(null, '', '#' + key); } catch (_) {}
        }
        window.scrollTo({ top: 0, behavior: 'auto' });
      }

      function jumpToSection(secId) {
        switchPage(secId, true);
      }

      (function initActivePage() {
        const rawHash = String(window.location.hash || '').replace(/^#/, '').trim();
        if (PAGE_META[rawHash] || SEC_TO_PAGE[rawHash]) {
          switchPage(rawHash, false);
          return;
        }
        try {
          const savedPage = localStorage.getItem('geb_active_page');
          if (savedPage && PAGE_META[savedPage]) {
            switchPage(savedPage, false);
            return;
          }
        } catch (_) {}
        switchPage('seats', false);
      })();

      window.addEventListener('hashchange', function () {
        const h = String(window.location.hash || '').replace(/^#/, '').trim();
        if (h && (PAGE_META[h] || SEC_TO_PAGE[h])) switchPage(h, false);
      });

      document.querySelectorAll('.side-nav-btn[data-nav-page]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          switchPage(btn.getAttribute('data-nav-page'), true);
        });
      });

      document.querySelectorAll('[data-jump-sec]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          jumpToSection(btn.getAttribute('data-jump-sec'));
        });
      });

      function switchAuditSubTab(tabKey) {
        activeAuditTab = tabKey === 'logs' ? 'logs' : 'sessions';
        document.getElementById('auditPaneSessions').hidden = (activeAuditTab !== 'sessions');
        document.getElementById('auditPaneLogs').hidden = (activeAuditTab !== 'logs');
        document.querySelectorAll('#auditSubTabs .seg-tab').forEach(function (btn) {
          const isAct = btn.getAttribute('data-audit-tab') === activeAuditTab;
          btn.classList.toggle('active', isAct);
          btn.setAttribute('aria-selected', isAct ? 'true' : 'false');
        });
      }

      document.getElementById('auditSubTabs').addEventListener('click', function (ev) {
        const btn = ev.target && ev.target.closest('.seg-tab');
        if (!btn) return;
        switchAuditSubTab(btn.getAttribute('data-audit-tab'));
      });

      function switchLedgerSubTab(tabKey) {
        activeLedgerTab = tabKey || 'tenants';
        document.getElementById('ledgerPaneTenants').hidden = (activeLedgerTab !== 'tenants');
        document.getElementById('ledgerPaneModels').hidden = (activeLedgerTab !== 'models');
        document.getElementById('ledgerPaneDaily').hidden = (activeLedgerTab !== 'daily');
        document.getElementById('ledgerPaneRecent').hidden = (activeLedgerTab !== 'recent');
        document.querySelectorAll('#ledgerSubTabs .seg-tab').forEach(function (btn) {
          btn.classList.toggle('active', btn.getAttribute('data-ledger-tab') === activeLedgerTab);
        });
      }

      document.getElementById('ledgerSubTabs').addEventListener('click', function (ev) {
        const btn = ev.target && ev.target.closest('.seg-tab');
        if (!btn) return;
        switchLedgerSubTab(btn.getAttribute('data-ledger-tab'));
      });

      function friendlyTenantLabel(tid, fallbackName) {
        if (!tid || tid === 'host') return '本机直连';
        if (tid === 'external_unauth') return '未授权外部探测';
        if (lastDistSnapshot && Array.isArray(lastDistSnapshot.clients)) {
          for (let i = 0; i < lastDistSnapshot.clients.length; i++) {
            const c = lastDistSnapshot.clients[i];
            if (c && (c.tenant_id === tid || ('client:' + c.client_id) === tid)) {
              return '密钥 · ' + (c.name || c.client_id);
            }
          }
        }
        const raw = String(fallbackName || tid);
        return raw.startsWith('客户端 · ') ? ('密钥 · ' + raw.slice(6)) : raw;
      }

      function syncTenantSelectOptions(selectEl, allLabel) {
        if (!selectEl) return;
        const prevVal = selectEl.value || 'all';
        const seen = { all: true, host: true };
        const items = [
          { value: 'all', label: allLabel || '全部汇总' },
          { value: 'host', label: '本机直连（免密）' }
        ];
        if (lastDistSnapshot && Array.isArray(lastDistSnapshot.clients)) {
          lastDistSnapshot.clients.forEach(function (c) {
            const tid = c.tenant_id || ('client:' + c.client_id);
            if (!seen[tid]) {
              seen[tid] = true;
              items.push({ value: tid, label: 'API · ' + (c.name || c.client_id) + (c.enabled ? '' : ' (已暂停)') });
            }
          });
        }
        const byTen = (lastUsageSnapshot && lastUsageSnapshot.by_tenant) || {};
        Object.keys(byTen).forEach(function (tid) {
          if (!seen[tid]) {
            seen[tid] = true;
            items.push({ value: tid, label: friendlyTenantLabel(tid, byTen[tid] && byTen[tid].tenant_name) });
          }
        });
        selectEl.replaceChildren();
        items.forEach(function (it) {
          const opt = document.createElement('option');
          opt.value = it.value;
          opt.textContent = it.label;
          selectEl.appendChild(opt);
        });
        selectEl.value = seen[prevVal] ? prevVal : 'all';
      }

      const PERIOD_LABELS = { today: '今日', yesterday: '昨日', '7d': '近 7 天', '30d': '近 30 天', session: '本次启动', all: '全部累计' };
      const TASK_META = {
        responses: { label: 'Copilot / 编程代理', color: '#2563eb' },
        chat: { label: '日常对话', color: '#10b981' },
        compaction: { label: '自动整理记忆', color: '#f59e0b' },
        image_gen: { label: '生成图片', color: '#8b5cf6' },
        image_edit: { label: '修改图片', color: '#ec4899' },
        video_gen: { label: '生成视频', color: '#06b6d4' },
        audio: { label: '语音处理', color: '#64748b' }
      };

      function syncCurrencyButtons() {
        document.querySelectorAll('#currencySwitch .cur-btn').forEach(function (btn) {
          btn.classList.toggle('active', btn.getAttribute('data-cur') === selectedCurrency);
        });
      }
      syncCurrencyButtons();

      document.getElementById('currencySwitch').addEventListener('click', function (ev) {
        ev.stopPropagation();
        const btn = ev.target && ev.target.closest('.cur-btn');
        if (!btn) return;
        const cur = btn.getAttribute('data-cur');
        if (!cur) return;
        selectedCurrency = cur;
        try { localStorage.setItem('geb_currency', cur); } catch (_) {}
        syncCurrencyButtons();
        if (lastUsageSnapshot) renderLedger(lastUsageSnapshot, lastCtxSnapshot);
      });

      function fmtTok(n) {
        const v = Number(n || 0);
        if (v >= 1000000) return (v / 1000000).toFixed(2) + 'M';
        if (v >= 1000) return (v / 1000).toFixed(1) + 'K';
        return String(Math.round(v));
      }

      function fmtLedgerTok(n) {
        const v = Number(n || 0);
        if (v >= 1000000) return (v / 1000000).toFixed(2) + 'M';
        if (v >= 10000) return (v / 1000).toFixed(1) + 'K';
        return Math.round(v).toLocaleString();
      }

      function resolveTokenParts(b) {
        const s = b || {};
        const cached = Number(s.cached_tokens || 0);
        const promptTot = Number(s.prompt_tokens || 0);
        const input = s.delta_prompt_tokens !== undefined ? Number(s.delta_prompt_tokens || 0) : Math.max(0, promptTot - cached);
        const output = Number(s.completion_tokens || 0);
        const total = Number(s.total_tokens || (input + output + cached));
        return { input: input, output: output, cached: cached, total: total };
      }

      function fmtMoney(usdVal, cnyVal) {
        if (selectedCurrency === 'USD') {
          const u = Number(usdVal || 0);
          return '$' + (u > 0 && u < 0.01 ? u.toFixed(4) : u.toFixed(2));
        }
        const c = Number(cnyVal !== undefined ? cnyVal : (Number(usdVal || 0) * 6.8));
        return '¥' + (c > 0 && c < 0.01 ? c.toFixed(4) : c.toFixed(2));
      }

      let dynamicExtraModels = [];

      function friendlyModel(raw) {
        const str = String(raw || '').trim();
        const s = str.toLowerCase();
        if (!s) return 'Carbon';
        if (s.includes('hydrogen')) return 'Hydrogen';
        if (s.includes('carbon')) return 'Carbon';
        if (s.includes('argon')) return 'Argon';
        if (s.includes('3.1-pro') || s === '3-pro' || s.includes('3.1')) return 'Gemini 3.1 Pro';
        if (s.includes('3.8-flash') || s === '3-flash' || s.includes('3.8')) return 'Gemini 3.8 Flash';
        if (s.includes('2.5-pro') || (s.includes('2.5') && s.includes('pro'))) return 'Gemini 2.5 Pro';
        if (s.includes('2.5-flash') || (s.includes('2.5') && s.includes('flash'))) return 'Gemini 2.5 Flash';
        if (s.includes('enterprise-web') || s === 'auto' || s === 'default') return 'Carbon';
        const stripped = str.replace(/^(?:gemini-web:|web:|gemini-)/i, '').trim();
        if (stripped && stripped !== str) {
          return stripped
            .split(/[\s_\-]+/)
            .filter(Boolean)
            .map(function (p) { return /^\d/.test(p) ? p : (p.charAt(0).toUpperCase() + p.slice(1)); })
            .join(' ');
        }
        return str;
      }

      function syncDynamicModelOptions(availableModels, defModel) {
        const sel = document.getElementById('modelSelect');
        if (!sel || !Array.isArray(availableModels)) return;
        const existingVals = new Set();
        Array.from(sel.querySelectorAll('option')).forEach(function (opt) {
          existingVals.add(String(opt.value || '').toLowerCase());
        });
        let dynGroup = document.getElementById('discoveredModelsGroup');
        const addedExtras = [];
        availableModels.forEach(function (mid) {
          const mStr = String(mid || '').trim();
          const mLow = mStr.toLowerCase();
          if (!mStr || mLow.endsWith('-newchat') || mLow === 'gemini-enterprise-gcloud') return;
          if (!existingVals.has(mLow)) {
            if (!dynGroup) {
              dynGroup = document.createElement('optgroup');
              dynGroup.id = 'discoveredModelsGroup';
              dynGroup.label = '自动发现的新模型';
              sel.appendChild(dynGroup);
            }
            const opt = document.createElement('option');
            opt.value = mStr;
            opt.textContent = friendlyModel(mStr) + '（自动适配）';
            dynGroup.appendChild(opt);
            existingVals.add(mLow);
          }
          if (
            mLow !== 'gemini-enterprise-web' &&
            mLow !== 'gemini-2.5-pro' &&
            mLow !== 'gemini-2.5-flash' &&
            mLow !== String(defModel || 'gemini-4-carbon').toLowerCase()
          ) {
            addedExtras.push(mStr);
          }
        });
        if (addedExtras.length > 0) {
          dynamicExtraModels = addedExtras;
          const otherEl = document.getElementById('copyOtherModels');
          if (otherEl) {
            otherEl.textContent = addedExtras.join(', ');
          }
        }
      }

      function friendlyStrategyBadge(strategy) {
        const s = String(strategy || '');
        const span = document.createElement('span');
        if (s === 'smart_incremental_delta') { span.className = 'chip ok'; span.textContent = '增量续聊'; return span; }
        if (s === 'smart_session_start' || s === 'smart_reset_mismatch') { span.className = 'chip busy'; span.textContent = '新建对话'; return span; }
        if (s === 'smart_packed_context') { span.className = 'chip purple'; span.textContent = '浓缩续接'; return span; }
        if (s === 'compaction_one_shot') { span.className = 'chip warn'; span.textContent = '自动浓缩'; return span; }
        span.className = 'chip'; span.textContent = '常规请求';
        return span;
      }

      function friendlyStrategyLabel(strategy) {
        const s = String(strategy || '');
        if (s === 'smart_incremental_delta') return '同对话增量发送';
        if (s === 'smart_session_start') return '新开启首轮对话';
        if (s === 'smart_packed_context') return '整理摘要后续聊';
        if (s === 'smart_reset_mismatch') return '切换话题重开页';
        if (s === 'compaction_one_shot') return '后台浓缩记忆';
        return '智能记忆管理';
      }

      function shortWorkLabel(phase, waitingCnt) {
        if (phase === 'running_tool') return '执行命令中';
        if (phase === 'waiting_model') return '等待回复';
        if (phase === 'generating') return '模型回复中';
        if (waitingCnt > 0) return '排队 ' + waitingCnt;
        return '工作中';
      }

      function friendlySeatLabel(slotKey, tabId) {
        const num = slotKey ? seatIndexBySlotKey[slotKey] : seatIndexByTabId[String(tabId)];
        if (num) return num + '号席位';
        if (tabId) return '工作页 #' + String(tabId).slice(-4);
        return '自动分配';
      }

      function makeCell(content, cls) {
        const td = document.createElement('td');
        if (cls) td.className = cls;
        if (content instanceof Node) td.appendChild(content);
        else td.textContent = String(content);
        return td;
      }

      function renderEmptyRow(tbody, cols, msg) {
        tbody.replaceChildren();
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = cols;
        td.className = 'muted';
        td.style.textAlign = 'center';
        td.style.padding = '22px 12px';
        td.textContent = msg;
        tr.appendChild(td);
        tbody.appendChild(tr);
      }

      function setStepState(cardId, iconId, descId, tagId, state, iconText, descText, tagText) {
        const card = document.getElementById(cardId);
        const icon = document.getElementById(iconId);
        const desc = document.getElementById(descId);
        const tag = document.getElementById(tagId);
        card.className = 'step-card ' + state;
        icon.textContent = iconText;
        desc.textContent = descText;
        tag.textContent = tagText;
        tag.className = 'chip ' + (state === 'ok' ? 'ok' : (state === 'warn' ? 'warn' : 'err'));
      }
      async function resetSingleSeat(slotKey, seatLabel, btn) {
        const origText = btn.textContent;
        btn.disabled = true;
        btn.textContent = '重置中...';
        try {
          const authVal = document.getElementById('apiKeyInput').value.trim();
          const headers = { 'Content-Type': 'application/json' };
          if (authVal) headers['Authorization'] = 'Bearer ' + authVal;
          const res = await fetch('/api/slots/reset', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({ slot_key: slotKey })
          });
          const data = await res.json().catch(function () { return {}; });
          if (!res.ok || !data.ok) {
            btn.textContent = (data && data.detail) ? '正忙稍后试' : '重置失败';
            setTimeout(function () {
              btn.textContent = origText;
              btn.disabled = false;
            }, 2000);
            return;
          }
          btn.textContent = seatLabel + '已换新 ✓';
          setTimeout(function () {
            btn.textContent = '开启新对话';
            btn.disabled = false;
            refreshStatus();
          }, 600);
        } catch (_) {
          btn.textContent = '网络异常';
          setTimeout(function () {
            btn.textContent = origText;
            btn.disabled = false;
          }, 1800);
        }
      }

      function renderSeatDetailPanel() {
        const emptyEl = document.getElementById('seatDetailEmpty');
        const contentEl = document.getElementById('seatDetailContent');
        const item = selectedSlotKey ? enrichedBySlotKey[selectedSlotKey] : null;
        if (!item) {
          selectedSlotKey = null;
          emptyEl.hidden = false;
          contentEl.hidden = true;
          return;
        }
        emptyEl.hidden = true;
        contentEl.hidden = false;

        const s = item.slot;
        const cs = item.cs;
        const usedTok = Number(cs.total_context_tokens || 0);
        const cmpTok = Number(cs.compact_threshold_tokens || 300000);
        const limTok = Number(cs.context_limit_tokens || 1048576);
        const remTok = cs.remaining_to_compact_tokens !== undefined
          ? Number(cs.remaining_to_compact_tokens)
          : Math.max(0, cmpTok - usedTok);
        const cmpPct = Math.min(100, Math.max(0, Number(cs.compact_pct || 0)));
        const utilPct = cs.utilization_pct !== undefined
          ? Number(cs.utilization_pct)
          : ((usedTok / Math.max(1, limTok)) * 100);

        document.getElementById('detailSeatBadge').textContent = friendlySeatLabel(s.slot_key, s.tab_id);
        const tenantChip = document.getElementById('detailTenantChip');
        if (item.isExternalTenant) {
          tenantChip.hidden = false;
          tenantChip.textContent = item.tenantLabel || '外部客户端';
        } else {
          tenantChip.hidden = true;
        }
        const titleEl = document.getElementById('detailSeatTitle');
        titleEl.textContent = item.topic || '暂无活跃会话 · 空闲待命页';
        titleEl.title = item.topic || '空闲待命页';

        const srcMap = { codex: '编程会话', prompt: '首条提问', web: '网页标题', task: '专项任务' };
        const srcChip = document.getElementById('detailSourceChip');
        srcChip.textContent = item.topic ? (srcMap[item.titleSource] || '活跃会话') : '空白待命';
        srcChip.className = item.topic ? 'chip purple' : 'chip';

        const stChip = document.getElementById('detailStatusChip');
        if (item.isWorking) {
          stChip.className = 'chip busy';
          stChip.textContent = item.activityLabel + (item.toolName ? (' · ' + item.toolName) : '');
        } else if (item.hasActiveConv) {
          stChip.className = 'chip purple';
          stChip.textContent = '会话待命';
        } else {
          stChip.className = 'chip ok';
          stChip.textContent = '空闲待命';
        }

        const rstBtn = document.getElementById('detailResetBtn');
        if (rstBtn.textContent !== '重置中...' && !rstBtn.textContent.includes('✓')) {
          rstBtn.disabled = Boolean(item.isWorking);
          rstBtn.textContent = item.isWorking ? '工作中不可重置' : '开启新对话';
        }

        document.getElementById('detailCtxLeft').textContent =
          '记忆池占用：已用 ' + fmtTok(usedTok) + ' / 自动整理线 ' + fmtTok(cmpTok) + ' (' + cmpPct.toFixed(1) + '%)';
        document.getElementById('detailCtxRight').textContent =
          '距自动整理还剩 ' + fmtTok(remTok) + ' · 总容量上限 ' + fmtTok(limTok) + ' (' + utilPct.toFixed(1) + '%)';
        const fill = document.getElementById('detailCtxFill');
        fill.style.width = cmpPct.toFixed(1) + '%';
        fill.className = cmpPct >= 100 ? 'ctx-fill danger' : (cmpPct >= 80 ? 'ctx-fill warn' : 'ctx-fill');

        const lastIn = Number(cs.last_delta_tokens || 0);
        const lastOut = Number(cs.last_completion_tokens || 0);
        document.getElementById('detailTurnIo').textContent =
          (lastIn > 0 || lastOut > 0) ? ('输入 +' + fmtTok(lastIn) + ' / 输出 ' + fmtTok(lastOut)) : '暂无本轮收发';
        document.getElementById('detailStrategy').textContent =
          friendlyStrategyLabel(cs.last_strategy) + ' · ' + friendlyModel(s.active_model || cs.active_model);

        const uTurns = Number(cs.user_turns || 0);
        const tSteps = Number(cs.turn_count || 0);
        const cmpCnt = Number(cs.compaction_count || 0);
        const completedReqs = Number(s.completed_requests || 0);
        document.getElementById('detailTurns').textContent = uTurns + ' 轮提问 · ' + tSteps + ' 步交互';
        document.getElementById('detailReqs').textContent =
          cmpCnt > 0 ? ('当前新网页步数 · 本页完成 ' + completedReqs + ' 次') : ('本页累计完成 ' + completedReqs + ' 次请求');

        const cmpEl = document.getElementById('detailCompactions');
        const cmpSubEl = document.getElementById('detailCompactionSub');
        if (cmpCnt > 0) {
          cmpEl.textContent = '已压缩 ' + cmpCnt + ' 次';
          cmpSubEl.textContent = '正在第 ' + (cmpCnt + 1) + ' 段新网页续聊';
        } else {
          cmpEl.textContent = '0 次（未压缩）';
          cmpSubEl.textContent = '满 ' + fmtTok(cmpTok) + ' 自动浓缩换新页';
        }

        const cleanWebTitle = String(s.tab_title || '').replace(/\s*-\s*Gemini Enterprise\s*$/i, '').trim();
        const webSessMatch = String(s.tab_url || '').match(/\/r\/session\/([^/?#]+)/i);
        const webSessShort = webSessMatch && webSessMatch[1] ? webSessMatch[1].slice(-6) : '';

        const sidEl = document.getElementById('detailSessionId');
        const effSid = s.pinned_session_id || cs.pinned_session_id;
        if (effSid) {
          const sid = String(effSid);
          sidEl.textContent = sid.length > 16 ? ('会话 ' + sid.slice(0, 8) + '…' + sid.slice(-4)) : ('会话 ' + sid);
          sidEl.title = '绑定会话 ID: ' + sid + (webSessShort ? ('\n当前网页对话尾号: #' + webSessShort) : '');
        } else {
          sidEl.textContent = '未绑定固定会话';
          sidEl.title = webSessShort ? ('当前网页对话尾号: #' + webSessShort) : '';
        }
        const tabShort = s.tab_id ? ('标签页 #' + String(s.tab_id).slice(-4)) : '自动工作页';
        const activeTimeStr = item.lastTs > 0 ? (new Date(item.lastTs * 1000).toLocaleTimeString() + ' 活跃') : '随时待命';
        const updEl = document.getElementById('detailUpdatedAt');
        updEl.textContent = tabShort + ' · ' + activeTimeStr;
        updEl.title = (s.tab_id ? ('浏览器标签页 ID: #' + s.tab_id) : '自动分配工作页')
          + (cleanWebTitle ? ('\n网页标题: ' + cleanWebTitle) : '')
          + (s.tab_ready === false ? '\n状态: 页面加载中' : '');
      }

      function buildSeatCard(item) {
        const s = item.slot;
        const cs = item.cs;
        const turns = item.turns;
        const cmpPct = Math.min(100, Math.max(0, Number(cs.compact_pct || 0)));
        const isSelected = Boolean(s.slot_key && selectedSlotKey === s.slot_key);

        const card = document.createElement('div');
        card.className = 'seat-card'
          + (item.isWorking ? ' busy' : (item.hasActiveConv ? ' active-idle' : ''))
          + (isSelected ? ' selected' : '');
        card.setAttribute('role', 'button');
        card.setAttribute('tabindex', '0');
        card.setAttribute('aria-pressed', isSelected ? 'true' : 'false');

        const top = document.createElement('div');
        top.className = 'seat-top';
        const nameWrap = document.createElement('div');
        nameWrap.className = 'seat-name';
        nameWrap.textContent = friendlySeatLabel(s.slot_key, s.tab_id);

        const stateChip = document.createElement('span');
        if (item.isWorking) {
          stateChip.className = 'chip busy';
          const pulse = document.createElement('i');
          pulse.className = 'live-work-dot';
          stateChip.appendChild(pulse);
          stateChip.appendChild(document.createTextNode(shortWorkLabel(item.activityPhase, item.waitingCnt)));
        } else if (item.hasActiveConv) {
          stateChip.className = 'chip purple';
          stateChip.textContent = '会话待命';
        } else {
          stateChip.className = 'chip ok';
          stateChip.textContent = '空闲待命';
        }
        top.appendChild(nameWrap);
        top.appendChild(stateChip);

        const topicDiv = document.createElement('div');
        if (item.topic) {
          topicDiv.className = 'seat-topic';
          topicDiv.textContent = item.topic;
          topicDiv.title = item.topic;
        } else {
          topicDiv.className = 'seat-topic empty';
          topicDiv.textContent = item.isWorking ? '正在处理当前请求…' : '暂无活跃会话 · 随时待命';
        }

        const meta = document.createElement('div');
        meta.className = 'seat-meta';
        const modelSpan = document.createElement('span');
        modelSpan.textContent = friendlyModel(s.active_model || cs.active_model);
        const turnSpan = document.createElement('span');
        const turnBase = turns > 0 ? ('第 ' + turns + ' 轮') : (item.isWorking ? '进行中' : '待命');
        turnSpan.textContent = item.isExternalTenant ? (item.shortTenantLabel + ' · ' + turnBase) : turnBase;
        if (item.isExternalTenant) {
          turnSpan.style.color = '#d97706';
          turnSpan.style.fontWeight = '650';
        }
        meta.appendChild(modelSpan);
        meta.appendChild(turnSpan);

        const track = document.createElement('div');
        track.className = 'seat-mini-track';
        const fill = document.createElement('div');
        fill.className = 'seat-mini-fill';
        fill.style.width = cmpPct.toFixed(1) + '%';
        if (cmpPct >= 85) fill.style.background = '#f59e0b';
        track.appendChild(fill);

        card.appendChild(top);
        card.appendChild(topicDiv);
        card.appendChild(meta);
        card.appendChild(track);

        function toggleSelect() {
          if (!s.slot_key) return;
          selectedSlotKey = (selectedSlotKey === s.slot_key) ? null : s.slot_key;
          document.querySelectorAll('.seat-card[data-slot-key]').forEach(function (el) {
            const active = el.getAttribute('data-slot-key') === selectedSlotKey;
            el.classList.toggle('selected', active);
            el.setAttribute('aria-pressed', active ? 'true' : 'false');
          });
          renderSeatDetailPanel();
        }
        if (s.slot_key) card.setAttribute('data-slot-key', s.slot_key);
        card.addEventListener('click', toggleSelect);
        card.addEventListener('keydown', function (ev) {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            toggleSelect();
          }
        });
        return card;
      }

      function renderSeatsGrid(slots, ctxMon) {
        const grid = document.getElementById('seatsGrid');
        const idleWrap = document.getElementById('idleSeatsWrap');
        const idleBody = document.getElementById('idleSeatsBody');
        const idleGrid = document.getElementById('idleSeatsGrid');
        const chip = document.getElementById('seatsSummaryChip');
        const sideBadge = document.getElementById('sideSeatsBadge');
        seatIndexByTabId = {};
        seatIndexBySlotKey = {};
        enrichedBySlotKey = {};

        const ctxSlotsMap = {};
        if (ctxMon && Array.isArray(ctxMon.slots)) {
          ctxMon.slots.forEach(function (cs) {
            if (cs && cs.slot_key) ctxSlotsMap[cs.slot_key] = cs;
          });
        }

        if (!Array.isArray(slots) || slots.length === 0) {
          chip.textContent = '0 个席位在线';
          chip.className = 'chip warn';
          if (sideBadge) { sideBadge.className = 'side-badge warn'; sideBadge.textContent = '0 页'; }
          idleWrap.hidden = true;
          selectedSlotKey = null;
          renderSeatDetailPanel();
          grid.replaceChildren();
          const empty = document.createElement('div');
          empty.className = 'empty-seats';
          empty.textContent = '当前还没有挂载 Google 工作页。请在 Chrome 打开并登录 vertexaisearch.cloud.google.com，席位会自动出现。';
          grid.appendChild(empty);
          return 0;
        }

        let busyCount = 0;
        const enrichedSlots = slots.map(function (s, idx) {
          const num = s.seat_index === undefined ? idx + 1 : s.seat_index;
          if (num && s.slot_key) seatIndexBySlotKey[s.slot_key] = num;
          if (num && s.tab_id) seatIndexByTabId[String(s.tab_id)] = num;
          const cs = ctxSlotsMap[s.slot_key] || {};
          const waitingCnt = Number(s.waiting_count || cs.waiting_count || 0);
          const sessionActive = Boolean(cs.session_active);
          const isWorking = Boolean(s.busy) || waitingCnt > 0 || sessionActive;
          if (isWorking) busyCount += 1;
          const activityPhase = String(
            cs.activity_phase || (s.busy ? 'generating' : (waitingCnt > 0 ? 'queued' : (isWorking ? 'waiting_model' : 'idle')))
          );
          const activityLabel = String(
            cs.activity_label || (s.busy ? '工作中 · 模型回复' : (waitingCnt > 0 ? ('工作中 · 排队 ' + waitingCnt) : (isWorking ? '工作中' : '空闲待命')))
          );
          const toolName = String(cs.tool_name || '');
          const topic = String(cs.session_title || '').trim();
          const turns = Number(cs.user_turns || cs.turn_count || 0);
          const usedTok = Number(cs.total_context_tokens || 0);
          const isEphemeralProbe = Boolean(cs.ephemeral_probe) && !isWorking;
          const hasActiveConv = !isEphemeralProbe && (Boolean(topic) || turns > 0 || usedTok > 0 || Boolean(s.pinned_session_id));
          const lastTs = Math.max(Number(cs.updated_at || 0), Number(s.last_used_at || 0));
          const tenantId = String(s.owner_tenant_id || cs.tenant_id || 'host');
          const isExternalTenant = Boolean(tenantId && tenantId !== 'host');
          const tenantLabel = String(cs.tenant_name || (isExternalTenant ? '外部客户端' : '本机直连'));
          const shortTenantLabel = tenantLabel.startsWith('客户端 · ') ? ('共享·' + tenantLabel.slice(6)) : tenantLabel;
          const enriched = {
            slot: s,
            cs: cs,
            seatNum: num || Number.MAX_SAFE_INTEGER,
            waitingCnt: waitingCnt,
            isWorking: isWorking,
            activityPhase: activityPhase,
            activityLabel: activityLabel,
            toolName: toolName,
            topic: topic,
            titleSource: String(cs.title_source || ''),
            turns: turns,
            hasActiveConv: hasActiveConv,
            lastTs: lastTs,
            tenantId: tenantId,
            isExternalTenant: isExternalTenant,
            tenantLabel: tenantLabel,
            shortTenantLabel: shortTenantLabel
          };
          if (s.slot_key) enrichedBySlotKey[s.slot_key] = enriched;
          return enriched;
        });

        enrichedSlots.sort(function (a, b) {
          const rankA = a.isWorking ? 0 : (a.hasActiveConv ? 1 : 2);
          const rankB = b.isWorking ? 0 : (b.hasActiveConv ? 1 : 2);
          if (rankA !== rankB) return rankA - rankB;
          if (rankA === 0) return a.seatNum - b.seatNum;
          if (rankA === 1 && b.lastTs !== a.lastTs) return b.lastTs - a.lastTs;
          return a.seatNum - b.seatNum;
        });

        const activeItems = enrichedSlots.filter(function (x) { return x.isWorking || x.hasActiveConv; });
        const idleItems = enrichedSlots.filter(function (x) { return !x.isWorking && !x.hasActiveConv; });

        if (busyCount > 0) {
          chip.textContent = activeItems.length + ' 活跃 (' + busyCount + ' 工作中) · ' + idleItems.length + ' 空闲';
          if (sideBadge) { sideBadge.className = 'side-badge busy'; sideBadge.textContent = busyCount + ' 忙 / ' + slots.length; }
        } else if (activeItems.length > 0) {
          chip.textContent = activeItems.length + ' 活跃 · ' + idleItems.length + ' 空闲';
          if (sideBadge) { sideBadge.className = 'side-badge ok'; sideBadge.textContent = slots.length + ' 页就绪'; }
        } else {
          chip.textContent = slots.length + ' 个空闲待命';
          if (sideBadge) { sideBadge.className = 'side-badge ok'; sideBadge.textContent = slots.length + ' 页待命'; }
        }
        chip.className = busyCount > 0 ? 'chip busy' : 'chip ok';

        grid.replaceChildren();
        if (activeItems.length === 0) {
          const empty = document.createElement('div');
          empty.className = 'empty-seats';
          empty.textContent = '当前暂无进行中的活跃对话 · 新请求将自动分配席位，也可展开下方空闲席位查看';
          grid.appendChild(empty);
        } else {
          activeItems.forEach(function (item) {
            grid.appendChild(buildSeatCard(item));
          });
        }

        if (idleItems.length === 0) {
          idleWrap.hidden = true;
          idleGrid.replaceChildren();
        } else {
          idleWrap.hidden = false;
          document.getElementById('idleSeatsLabel').textContent = '空闲待命席位（' + idleItems.length + ' 个）';
          document.getElementById('idleSeatsHint').textContent = idleSeatsExpanded ? '收起列表 ▴' : '展开查看 ▾';
          document.getElementById('idleSeatsToggle').setAttribute('aria-expanded', idleSeatsExpanded ? 'true' : 'false');
          idleBody.hidden = !idleSeatsExpanded;
          idleGrid.replaceChildren();
          idleItems.forEach(function (item) {
            idleGrid.appendChild(buildSeatCard(item));
          });
        }

        renderSeatDetailPanel();
        return busyCount;
      }
      function syncPeriodControls(usage) {
        const dailyMap = (usage && usage.daily) || {};
        const dayKeys = Object.keys(dailyMap).sort();
        const picker = document.getElementById('datePickerInput');
        if (dayKeys.length > 0) {
          picker.min = dayKeys[0];
          picker.max = dayKeys[dayKeys.length - 1];
        }
        if (selectedPeriod.startsWith('day:')) {
          const dk = selectedPeriod.slice(4);
          if (!dailyMap[dk]) {
            selectedPeriod = 'all';
            picker.value = '';
          } else {
            picker.value = dk;
          }
        } else {
          picker.value = '';
        }
        document.querySelectorAll('#periodTabs .period-tab').forEach(function (btn) {
          btn.classList.toggle('active', btn.getAttribute('data-period') === selectedPeriod);
        });
      }

      function resolveSelectedGroup(usage) {
        const periods = (usage && usage.periods) || {};
        const daily = (usage && usage.daily) || {};
        let baseRes = null;
        if (selectedPeriod.startsWith('day:')) {
          const dk = selectedPeriod.slice(4);
          if (daily[dk]) baseRes = { group: daily[dk], label: dk, filterDay: dk, mode: 'day' };
        }
        if (!baseRes && periods[selectedPeriod]) {
          let lbl = PERIOD_LABELS[selectedPeriod] || selectedPeriod;
          if (selectedPeriod === 'today' && usage.today_date) lbl = '今日 (' + usage.today_date.slice(5) + ')';
          if (selectedPeriod === 'yesterday' && usage.yesterday_date) lbl = '昨日 (' + usage.yesterday_date.slice(5) + ')';
          baseRes = {
            group: periods[selectedPeriod],
            label: lbl,
            filterDay: selectedPeriod === 'today' ? usage.today_date : (selectedPeriod === 'yesterday' ? usage.yesterday_date : null),
            mode: selectedPeriod
          };
        }
        if (!baseRes) {
          baseRes = {
            group: periods.all || { summary: usage.all_time || {}, by_model: usage.by_model || {}, by_task: usage.by_task || {}, by_tenant: usage.by_tenant || {} },
            label: '全部累计',
            filterDay: null,
            mode: 'all'
          };
        }
        const baseByTenant = (baseRes.group && baseRes.group.by_tenant) || {};
        if (selectedTenant && selectedTenant !== 'all') {
          const tGroup = baseByTenant[selectedTenant] || { summary: {}, by_model: {}, by_task: {}, errors: 0 };
          return {
            group: tGroup,
            baseByTenant: baseByTenant,
            label: baseRes.label + ' · ' + friendlyTenantLabel(selectedTenant, tGroup.tenant_name),
            filterDay: baseRes.filterDay,
            mode: baseRes.mode
          };
        }
        return { group: baseRes.group, baseByTenant: baseByTenant, label: baseRes.label, filterDay: baseRes.filterDay, mode: baseRes.mode };
      }

      function renderDailyBars(usage, activeFilterDay) {
        const stage = document.getElementById('dailyBarsStage');
        const dailyMap = (usage && usage.daily) || {};
        const existingDays = Object.keys(dailyMap).sort();
        const anchorStr = (usage && usage.today_date) || existingDays[existingDays.length - 1] || '';
        const daySet = {};
        existingDays.forEach(function (d) { daySet[d] = true; });
        if (anchorStr) {
          const anchorMs = Date.parse(anchorStr + 'T00:00:00Z');
          if (!isNaN(anchorMs)) {
            for (let i = 6; i >= 0; i--) {
              const dt = new Date(anchorMs - i * 86400000);
              daySet[dt.toISOString().slice(0, 10)] = true;
            }
          }
        }
        const days = Object.keys(daySet).sort().slice(-10);
        stage.replaceChildren();
        if (days.length === 0) {
          const empty = document.createElement('div');
          empty.className = 'muted';
          empty.style.margin = 'auto';
          empty.style.fontSize = '0.82rem';
          empty.textContent = '暂无按日趋势数据';
          stage.appendChild(empty);
          return;
        }

        let maxTotal = 0.0001;
        function pickDaySummary(dk) {
          const dObj = dailyMap[dk] || {};
          if (selectedTenant && selectedTenant !== 'all') {
            const tObj = (dObj.by_tenant && dObj.by_tenant[selectedTenant]) || {};
            return tObj.summary || {};
          }
          return dObj.summary || {};
        }
        days.forEach(function (dk) {
          const s = pickDaySummary(dk);
          const tot = selectedCurrency === 'USD' ? Number(s.cached_equivalent_cost_usd || 0) : Number(s.cached_equivalent_cost_cny || 0);
          if (tot > maxTotal) maxTotal = tot;
        });

        days.forEach(function (dk) {
          const s = pickDaySummary(dk);
          const actual = selectedCurrency === 'USD' ? Number(s.cached_equivalent_cost_usd || 0) : Number(s.cached_equivalent_cost_cny || 0);
          const reqCnt = Number(s.requests || 0);
          const actualH = reqCnt > 0 ? Math.max(8, (actual / maxTotal) * 100) : 3;
          const tp = resolveTokenParts(s);

          const col = document.createElement('div');
          col.className = 'bar-col' + (activeFilterDay === dk ? ' selected' : '');
          col.title = reqCnt > 0
            ? (dk + ' · ' + reqCnt + ' 次调用\n等效花费: ' + fmtMoney(s.cached_equivalent_cost_usd, s.cached_equivalent_cost_cny) +
              '\n缓存命中率: ' + Number(s.cache_hit_pct || 0).toFixed(1) + '%\n累计 Token: ' + fmtLedgerTok(tp.total))
            : (dk + ' · 无调用记录');

          const valTop = document.createElement('span');
          valTop.className = 'bar-val';
          valTop.textContent = reqCnt > 0 ? fmtMoney(s.cached_equivalent_cost_usd, s.cached_equivalent_cost_cny) : '';

          const stack = document.createElement('div');
          stack.className = 'bar-stack';
          const segActual = document.createElement('div');
          segActual.className = 'bar-seg-actual';
          segActual.style.height = actualH.toFixed(1) + '%';
          stack.appendChild(segActual);

          const lbl = document.createElement('span');
          lbl.className = 'bar-lbl';
          lbl.textContent = dk.slice(5);

          col.appendChild(valTop);
          col.appendChild(stack);
          col.appendChild(lbl);
          if (reqCnt > 0) {
            col.addEventListener('click', function () {
              selectedPeriod = (selectedPeriod === 'day:' + dk) ? 'all' : ('day:' + dk);
              if (lastUsageSnapshot) renderLedger(lastUsageSnapshot, lastCtxSnapshot);
            });
          } else {
            col.style.cursor = 'default';
          }
          stage.appendChild(col);
        });
      }

      function renderTaskBreakdown(byTask, totalRequests) {
        const bar = document.getElementById('taskRatioBar');
        const list = document.getElementById('taskBreakdownList');
        bar.replaceChildren();
        list.replaceChildren();

        const entries = Object.keys(byTask || {})
          .map(function (k) {
            const item = byTask[k] || {};
            const meta = TASK_META[k] || { label: item.label || k, color: '#94a3b8' };
            return {
              key: k,
              label: meta.label,
              color: meta.color,
              requests: Number(item.requests || 0),
              usd: Number(item.cached_equivalent_cost_usd || 0),
              cny: Number(item.cached_equivalent_cost_cny || (Number(item.cached_equivalent_cost_usd || 0) * 6.8))
            };
          })
          .filter(function (x) { return x.requests > 0; })
          .sort(function (a, b) { return b.requests - a.requests; });

        if (entries.length === 0 || totalRequests <= 0) {
          const empty = document.createElement('div');
          empty.className = 'muted';
          empty.style.fontSize = '0.82rem';
          empty.textContent = '当前时间段暂无调用记录';
          list.appendChild(empty);
          return;
        }

        entries.forEach(function (e) {
          const pct = Math.max(2, (e.requests / totalRequests) * 100);
          const seg = document.createElement('div');
          seg.style.width = pct.toFixed(1) + '%';
          seg.style.background = e.color;
          seg.title = e.label + ': ' + e.requests + ' 次';
          bar.appendChild(seg);

          const row = document.createElement('div');
          row.className = 'task-row';
          const left = document.createElement('div');
          left.className = 'task-row-left';
          const dot = document.createElement('i');
          dot.className = 'leg-dot';
          dot.style.background = e.color;
          const name = document.createElement('span');
          name.textContent = e.label;
          left.appendChild(dot);
          left.appendChild(name);

          const right = document.createElement('div');
          right.className = 'task-row-right';
          right.textContent = e.requests + ' 次 (' + Math.round((e.requests / totalRequests) * 100) + '%) · ' + fmtMoney(e.usd, e.cny);
          row.appendChild(left);
          row.appendChild(right);
          list.appendChild(row);
        });
      }

      function renderLedger(usage, ctxMon) {
        if (!usage) return;
        lastUsageSnapshot = usage;
        lastCtxSnapshot = ctxMon;
        syncPeriodControls(usage);
        const tenantSel = document.getElementById('ledgerTenantSelect');
        syncTenantSelectOptions(tenantSel, '全部汇总（本机 + 所有外部密钥）');
        if (tenantSel) tenantSel.value = selectedTenant;
        const resetTenBtn = document.getElementById('ledgerResetTenantFilterBtn');
        if (resetTenBtn) resetTenBtn.hidden = (selectedTenant === 'all');

        const todaySum = (usage.periods && usage.periods.today && usage.periods.today.summary) || {};
        const allSum = (usage.periods && usage.periods.all && usage.periods.all.summary) || usage.all_time || {};
        const ovLVal = document.getElementById('ovLedgerVal');
        const ovLSub = document.getElementById('ovLedgerSub');
        if (ovLVal) ovLVal.textContent = fmtMoney(todaySum.cached_equivalent_cost_usd, todaySum.cached_equivalent_cost_cny);
        if (ovLSub) ovLSub.textContent = '今日 ' + Number(todaySum.requests || 0).toLocaleString() + ' 次 · 累计 ' + Number(allSum.requests || 0).toLocaleString() + ' 次';
        const sideLBadge = document.getElementById('sideLedgerBadge');
        if (sideLBadge) sideLBadge.textContent = fmtMoney(allSum.cached_equivalent_cost_usd, allSum.cached_equivalent_cost_cny);

        const resolved = resolveSelectedGroup(usage);
        const curSummary = (resolved.group && resolved.group.summary) || {};
        const byModel = (resolved.group && resolved.group.by_model) || {};
        const byTask = (resolved.group && resolved.group.by_task) || {};
        const ap = usage.active_pricing || {};
        const fx = Number(usage.usd_to_cny_rate || 6.8);

        if (ap.display_name) {
          const rParts = (ap.input_rate_per_1m !== undefined && ap.cached_input_rate_per_1m !== undefined && ap.output_rate_per_1m !== undefined)
            ? (' ($' + ap.input_rate_per_1m + ' / $' + ap.cached_input_rate_per_1m + ' / $' + ap.output_rate_per_1m + ')')
            : '';
          document.getElementById('ledgerRateBadge').textContent = friendlyModel(ap.display_name) + rParts + ' · 汇率 ' + fx;
        }

        document.getElementById('activePeriodTag').textContent = resolved.label;
        document.getElementById('ledgerActualMain').textContent = fmtMoney(curSummary.cached_equivalent_cost_usd, curSummary.cached_equivalent_cost_cny);
        const actualSubEl = document.getElementById('ledgerActualSub');
        actualSubEl.textContent = '输入 / 缓存 / 输出单价折算';
        actualSubEl.title = '按每轮新增输入、同对话缓存与回复输出参考单价折算（非网页真实扣费）';

        const reqCount = Number(curSummary.requests || 0);
        const respCnt = (byTask.responses && byTask.responses.requests) || 0;
        const chatCnt = (byTask.chat && byTask.chat.requests) || 0;
        const cmpCnt = Number(curSummary.compactions || 0);
        const mediaCnt = Number(curSummary.images_generated || 0) + Number(curSummary.videos_generated || 0) + Number(curSummary.audio_requests || 0);
        document.getElementById('ledgerCallsMain').textContent = reqCount.toLocaleString() + ' 次';
        let callsThirdLabel = '整理 ' + cmpCnt;
        if (cmpCnt > 0 && mediaCnt > 0) callsThirdLabel = '专项 ' + (cmpCnt + mediaCnt);
        else if (mediaCnt > 0) callsThirdLabel = '媒体 ' + mediaCnt;
        const callsSubEl = document.getElementById('ledgerCallsSub');
        callsSubEl.textContent = '编程 ' + respCnt + ' / 对话 ' + chatCnt + ' / ' + callsThirdLabel;

        const sumTok = resolveTokenParts(curSummary);
        const promptTokTotal = sumTok.input + sumTok.cached;
        const hitPct = curSummary.cache_hit_pct !== undefined ? Number(curSummary.cache_hit_pct || 0) : (promptTokTotal > 0 ? (sumTok.cached / promptTokTotal) * 100 : 0);
        document.getElementById('ledgerCacheMain').textContent = hitPct.toFixed(1) + '%';
        document.getElementById('ledgerCacheSub').textContent = '缓存 ' + fmtLedgerTok(sumTok.cached) + ' / 总输入 ' + fmtLedgerTok(promptTokTotal);

        document.getElementById('ledgerTokensMain').textContent = fmtLedgerTok(sumTok.total);
        document.getElementById('ledgerTokensSub').textContent = '输入 ' + fmtLedgerTok(sumTok.input) + ' / 输出 ' + fmtLedgerTok(sumTok.output) + ' / 缓存 ' + fmtLedgerTok(sumTok.cached);

        renderDailyBars(usage, resolved.filterDay);
        renderTaskBreakdown(byTask, reqCount);

        const tenantBody = document.getElementById('ledgerTenantBody');
        const baseByTenant = resolved.baseByTenant || {};
        const tenantIds = Object.keys(baseByTenant);
        if (lastDistSnapshot && Array.isArray(lastDistSnapshot.clients)) {
          lastDistSnapshot.clients.forEach(function (c) {
            const tid = c.tenant_id || ('client:' + c.client_id);
            if (tenantIds.indexOf(tid) === -1) tenantIds.push(tid);
          });
        }
        if (tenantIds.length === 0) {
          renderEmptyRow(tenantBody, 7, '当前时间范围暂无分密钥调用记录');
        } else {
          tenantBody.replaceChildren();
          tenantIds.forEach(function (tid) {
            const tRaw = baseByTenant[tid] || { summary: {}, errors: 0, tenant_name: '' };
            const ts = tRaw.summary || {};
            const tt = resolveTokenParts(ts);
            const errCnt = Number(tRaw.errors !== undefined ? tRaw.errors : (ts.errors || 0));
            const tr = document.createElement('tr');
            if (selectedTenant === tid) tr.style.background = '#eff6ff';
            const nameWrap = document.createElement('div');
            nameWrap.style.display = 'flex';
            nameWrap.style.alignItems = 'center';
            nameWrap.style.gap = '6px';
            const bTag = document.createElement('span');
            bTag.className = tid === 'host' ? 'chip ok' : (tid === 'external_unauth' ? 'chip err' : 'chip purple');
            bTag.textContent = tid === 'host' ? '本机' : (tid === 'external_unauth' ? '陌生来源' : '外部 API');
            const tStrong = document.createElement('strong');
            tStrong.textContent = friendlyTenantLabel(tid, tRaw.tenant_name);
            nameWrap.appendChild(bTag);
            nameWrap.appendChild(tStrong);
            tr.appendChild(makeCell(nameWrap));
            tr.appendChild(makeCell((ts.requests || 0) + ' 次', 'num'));
            const errChip = document.createElement('span');
            errChip.className = errCnt > 0 ? 'chip warn' : 'muted';
            errChip.textContent = errCnt > 0 ? (errCnt + ' 次拦截/异常') : '0';
            tr.appendChild(makeCell(errChip, 'num'));
            tr.appendChild(makeCell(fmtLedgerTok(tt.total), 'num'));
            tr.appendChild(makeCell(fmtLedgerTok(tt.input) + ' / ' + fmtLedgerTok(tt.output) + ' / ' + fmtLedgerTok(tt.cached)));
            tr.appendChild(makeCell(fmtMoney(ts.cached_equivalent_cost_usd, ts.cached_equivalent_cost_cny), 'num'));
            const fBtn = document.createElement('button');
            fBtn.type = 'button';
            fBtn.className = selectedTenant === tid ? 'btn sm primary' : 'btn sm soft';
            fBtn.textContent = selectedTenant === tid ? '正在筛选 ✓' : '只看此项';
            fBtn.addEventListener('click', function () {
              selectedTenant = (selectedTenant === tid) ? 'all' : tid;
              renderLedger(lastUsageSnapshot, lastCtxSnapshot);
            });
            tr.appendChild(makeCell(fBtn));
            tenantBody.appendChild(tr);
          });
        }

        const modelBody = document.getElementById('ledgerModelBody');
        const modelKeys = Object.keys(byModel);
        if (modelKeys.length === 0) {
          renderEmptyRow(modelBody, 6, '当前时间范围暂无模型记录');
        } else {
          modelBody.replaceChildren();
          modelKeys.forEach(function (mk) {
            const m = byModel[mk] || {};
            const tr = document.createElement('tr');
            const specials = [];
            if (m.compactions > 0) specials.push('整理 ' + m.compactions);
            if (m.images_generated > 0) specials.push('生图 ' + m.images_generated);
            if (m.videos_generated > 0) specials.push('视频 ' + m.videos_generated);
            if (m.audio_requests > 0) specials.push('语音 ' + m.audio_requests);
            const mt = resolveTokenParts(m);
            tr.appendChild(makeCell(friendlyModel(m.display_name || mk)));
            tr.appendChild(makeCell((m.requests || 0) + ' 次', 'num'));
            tr.appendChild(makeCell(fmtLedgerTok(mt.total), 'num'));
            tr.appendChild(makeCell(fmtLedgerTok(mt.input) + ' / ' + fmtLedgerTok(mt.output) + ' / ' + fmtLedgerTok(mt.cached)));
            tr.appendChild(makeCell(specials.length ? specials.join(' · ') : '-'));
            tr.appendChild(makeCell(fmtMoney(m.cached_equivalent_cost_usd, m.cached_equivalent_cost_cny), 'num'));
            modelBody.appendChild(tr);
          });
        }

        const dailyBody = document.getElementById('ledgerDailyBody');
        const dailyObj = usage.daily || {};
        const sortedDays = Object.keys(dailyObj).sort().reverse();
        if (sortedDays.length === 0) {
          renderEmptyRow(dailyBody, 6, '暂无按日记录');
        } else {
          dailyBody.replaceChildren();
          sortedDays.forEach(function (dk) {
            const dRaw = dailyObj[dk] || {};
            const s = (selectedTenant && selectedTenant !== 'all')
              ? ((dRaw.by_tenant && dRaw.by_tenant[selectedTenant] && dRaw.by_tenant[selectedTenant].summary) || {})
              : (dRaw.summary || {});
            if (selectedTenant && selectedTenant !== 'all' && !(s.requests > 0)) return;
            const tr = document.createElement('tr');
            if (resolved.filterDay === dk) tr.style.background = '#eff6ff';
            const specials = [];
            if (s.compactions > 0) specials.push('整理 ' + s.compactions);
            if (s.images_generated > 0) specials.push('图 ' + s.images_generated);
            if (s.videos_generated > 0) specials.push('视频 ' + s.videos_generated);
            if (s.audio_requests > 0) specials.push('音 ' + s.audio_requests);
            const dt = resolveTokenParts(s);
            tr.appendChild(makeCell(dk));
            tr.appendChild(makeCell((s.requests || 0) + ' 次', 'num'));
            tr.appendChild(makeCell(fmtLedgerTok(dt.total), 'num'));
            tr.appendChild(makeCell(fmtLedgerTok(dt.input) + ' / ' + fmtLedgerTok(dt.output) + ' / ' + fmtLedgerTok(dt.cached)));
            tr.appendChild(makeCell(specials.length ? specials.join(' · ') : '-'));
            tr.appendChild(makeCell(fmtMoney(s.cached_equivalent_cost_usd, s.cached_equivalent_cost_cny), 'num'));
            dailyBody.appendChild(tr);
          });
          if (!dailyBody.children.length) renderEmptyRow(dailyBody, 6, '所选对象暂无按日记录');
        }

        const recentBody = document.getElementById('ledgerRecentBody');
        const allRecents = usage.recent_requests || [];
        const filteredRecents = allRecents.filter(function (r) {
          if (selectedTenant && selectedTenant !== 'all' && (r.tenant_id || 'host') !== selectedTenant) return false;
          if (resolved.filterDay) return r.day === resolved.filterDay;
          if (resolved.mode === '7d' && usage.today_date) {
            const cutoff = new Date(Date.parse(usage.today_date + 'T00:00:00Z') - 6 * 86400000).toISOString().slice(0, 10);
            return (r.day || '') >= cutoff;
          }
          if (resolved.mode === '30d' && usage.today_date) {
            const cutoff = new Date(Date.parse(usage.today_date + 'T00:00:00Z') - 29 * 86400000).toISOString().slice(0, 10);
            return (r.day || '') >= cutoff;
          }
          return true;
        });
        if (filteredRecents.length === 0) {
          renderEmptyRow(recentBody, 7, '该时间范围暂无调用流水');
        } else {
          recentBody.replaceChildren();
          filteredRecents.slice(0, 50).forEach(function (r) {
            const tr = document.createElement('tr');
            const tsStr = r.ts ? new Date(r.ts * 1000).toLocaleTimeString() : '-';
            const tMeta = TASK_META[r.task_type] || { label: r.task_label || '调用' };
            const seatTxt = friendlySeatLabel(r.slot_key, r.tab_id);
            const rt = resolveTokenParts(r);
            const callerPrefix = (r.tenant_id && r.tenant_id !== 'host') ? ('[' + friendlyTenantLabel(r.tenant_id, r.tenant_name) + '] ') : '';
            tr.appendChild(makeCell(tsStr));
            tr.appendChild(makeCell(callerPrefix + tMeta.label));
            tr.appendChild(makeCell(friendlyModel(r.display_name || r.model) + ' · ' + seatTxt));
            tr.appendChild(makeCell(friendlyStrategyBadge(r.context_strategy)));
            tr.appendChild(makeCell(fmtLedgerTok(rt.total) + '（' + fmtLedgerTok(rt.input) + ' / ' + fmtLedgerTok(rt.output) + ' / ' + fmtLedgerTok(rt.cached) + '）'));
            tr.appendChild(makeCell(fmtMoney(r.cached_equivalent_cost_usd, r.cached_equivalent_cost_cny), 'num'));
            tr.appendChild(makeCell(((Number(r.duration_ms || 0)) / 1000).toFixed(1) + ' 秒', 'num'));
            recentBody.appendChild(tr);
          });
        }
      }
      async function refreshStatus(isManual) {
        const manual = isManual === true;
        const refBtn = document.getElementById('refreshBtn');
        const refTxt = document.getElementById('refreshBtnText');
        const badge = document.getElementById('connBadge');
        const t0 = performance.now();
        if (manual) {
          if (refreshResetTimer) clearTimeout(refreshResetTimer);
          refBtn.classList.remove('is-refreshed', 'is-error');
          refBtn.classList.add('is-refreshing');
          refTxt.textContent = '同步中';
        }
        let refreshOk = false;
        try {
          const res = await fetch('/api/status');
          const data = await res.json();
          const dot = document.getElementById('connDot');
          const txt = document.getElementById('connText');
          const hub = data.hub || {};
          const clients = hub.clients || [];
          const slots = hub.worker_slots || [];
          const active = hub.active_client;
          const defModelName = friendlyModel(data.default_model || 'gemini-4-carbon');

          const ovMVal = document.getElementById('ovModelVal');
          if (ovMVal) ovMVal.textContent = defModelName + ' 主力';
          const sideProbe = document.getElementById('sideProbeBadge');
          if (sideProbe) sideProbe.textContent = defModelName;

          setStepState(
            'stepServiceCard', 'stepServiceIcon', 'stepServiceDesc', 'stepServiceTag',
            'ok', '✓', window.location.host + ' · ' + defModelName, '在线'
          );

          const extOnline = clients.length > 0 || slots.length > 0 || Boolean(active);
          const rawExtCap = (active && active.max_parallel_tabs !== undefined && active.max_parallel_tabs !== null)
            ? Number(active.max_parallel_tabs)
            : (hub.client_max_parallel_tabs !== undefined && hub.client_max_parallel_tabs !== null)
              ? Number(hub.client_max_parallel_tabs)
              : (hub.max_parallel_tabs !== undefined && hub.max_parallel_tabs !== null)
                ? Number(hub.max_parallel_tabs)
                : 10;
          const extParallelCap = (Number.isFinite(rawExtCap) && rawExtCap >= 1) ? Math.min(200, Math.round(rawExtCap)) : 10;
          if (!seatCapSaving && !seatCapCustomEditing) syncDashSeatCapUI(extParallelCap);
          const extCapLabel = '上限 ' + extParallelCap + ' 路并行';
          const transportCapLabel = '上限 ' + extParallelCap + ' 并发';
          const authBtn = document.getElementById('stepAuthBtn');
          const workingTotal = renderSeatsGrid(slots, data.context_monitor) || 0;

          if (!extOnline) {
            setStepState('stepExtCard', 'stepExtIcon', 'stepExtDesc', 'stepExtTag', 'warn', '!', '请开启浏览器扩展', '待连接');
            setStepState('stepAuthCard', 'stepAuthIcon', 'stepAuthDesc', 'stepAuthTag', 'warn', '3', '等待插件识别', '待检测');
            authBtn.hidden = false;
            dot.className = 'dot err';
            txt.textContent = '等待插件连接';
          } else if (slots.length === 0) {
            setStepState('stepExtCard', 'stepExtIcon', 'stepExtDesc', 'stepExtTag', 'ok', '✓', extCapLabel, '已连接');
            setStepState('stepAuthCard', 'stepAuthIcon', 'stepAuthDesc', 'stepAuthTag', 'warn', '!', '请打开并登录网页', '待打开');
            authBtn.hidden = false;
            dot.className = 'dot';
            txt.textContent = '请打开工作页';
          } else {
            setStepState('stepExtCard', 'stepExtIcon', 'stepExtDesc', 'stepExtTag', 'ok', '✓', extCapLabel, '已连接');
            setStepState('stepAuthCard', 'stepAuthIcon', 'stepAuthDesc', 'stepAuthTag', 'ok', '✓', slots.length + ' 页已登录', '就绪');
            authBtn.hidden = true;
            dot.className = 'dot ok';
            txt.textContent = workingTotal > 0 ? (workingTotal + ' 工作中 · 共 ' + slots.length + ' 页') : (slots.length + ' 页就绪待命');
          }

          lastDefaultModel = data.default_model || 'gemini-4-carbon';
          if (Array.isArray(data.available_models)) {
            syncDynamicModelOptions(data.available_models, lastDefaultModel);
          }
          if (data.distribution) renderDistribution(data.distribution, lastDefaultModel);
          if (data.tunnel) renderTunnel(data.tunnel, data.distribution);
          if (data.usage) renderLedger(data.usage, data.context_monitor);
          lastSessionsOverview = Array.isArray(data.sessions_overview) ? data.sessions_overview : [];
          renderAuditCenter(data.distribution, data.usage, lastSessionsOverview);

          document.getElementById('modeVal').textContent = '浏览器桥接 · 智能增量记忆';
          document.getElementById('transportVal').textContent = extOnline ? ('WebSocket 实时双工 · ' + transportCapLabel) : '未连接';
          document.getElementById('rpcVal').textContent = (active && active.captured_rpc) ? '已捕获流式通道' : '页面组件与流式双监听';
          const effBusy = Math.max(Number(hub.working_slots || 0), Number(hub.active_slots_busy || 0), workingTotal);
          document.getElementById('statsVal').textContent = effBusy + ' 工作中 / ' + (hub.queued_requests || 0) + ' 排队 / ' + (hub.total_requests || 0) + ' 累计';
          document.getElementById('diagStatsChip').textContent = (hub.total_requests || 0) + ' 次';

          const logs = hub.recent_logs || [];
          const logsBox = document.getElementById('logsBox');
          if (logs.length === 0) {
            logsBox.textContent = '暂无运行记录';
          } else {
            const prevScroll = logsBox.scrollTop;
            logsBox.replaceChildren();
            logs.slice().reverse().forEach(function (item) {
              const div = document.createElement('div');
              div.className = 'log-item';
              const t = new Date(item.ts * 1000).toLocaleTimeString();
              div.textContent = '[' + t + '] ' + item.message;
              logsBox.appendChild(div);
            });
            logsBox.scrollTop = prevScroll;
          }
          refreshOk = true;
        } catch (_) {
          document.getElementById('connDot').className = 'dot err';
          document.getElementById('connText').textContent = '本地服务未响应';
          setStepState('stepServiceCard', 'stepServiceIcon', 'stepServiceDesc', 'stepServiceTag', 'err', '✕', window.location.host + ' 未响应', '离线');
        }
        if (manual) {
          const waitMs = Math.max(0, 420 - (performance.now() - t0));
          await new Promise(function (resolve) { setTimeout(resolve, waitMs); });
          refBtn.classList.remove('is-refreshing');
          refBtn.classList.add(refreshOk ? 'is-refreshed' : 'is-error');
          refTxt.textContent = refreshOk ? '已同步 ✓' : '重试';
          if (refreshOk) {
            badge.classList.add('flash');
            setTimeout(function () { badge.classList.remove('flash'); }, 600);
          }
          refreshResetTimer = setTimeout(function () {
            refBtn.classList.remove('is-refreshed', 'is-error');
            refTxt.textContent = '刷新';
          }, 950);
        }
      }

      async function executeTestRequest(isQuickProbe) {
        const box = document.getElementById('probeResultBox');
        const resTitle = document.getElementById('probeResTitle');
        const resMeta = document.getElementById('probeResMeta');
        const out = document.getElementById('replyOut');
        const quickBtn = document.getElementById('quickProbeBtn');
        const customBtn = document.getElementById('sendBtn');
        const chosenModel = document.getElementById('modelSelect').value || 'gemini-4-carbon';
        const modelName = friendlyModel(chosenModel);

        const prompt = isQuickProbe
          ? '你好，这是一次连通性体检，请仅用简短中文回复“连接正常”即可。'
          : document.getElementById('promptInput').value.trim();

        if (!prompt) {
          box.hidden = false;
          box.className = 'probe-result err';
          resTitle.textContent = '请先输入测试内容';
          resMeta.textContent = '提示';
          out.textContent = '自定义测试需要填写问题；如果不想打字，直接点击上方的“一键快速体检”即可。';
          return;
        }

        quickBtn.disabled = true;
        customBtn.disabled = true;
        box.hidden = false;
        box.className = 'probe-result';
        resTitle.textContent = '正在测试 ' + modelName + ' ...';
        resMeta.textContent = '等待回复中';
        resMeta.className = 'chip busy';
        out.textContent = '';

        const t0 = performance.now();
        const authVal = document.getElementById('apiKeyInput').value.trim();
        const headers = {};
        if (authVal) headers['Authorization'] = 'Bearer ' + authVal;

        try {
          let messageContent = prompt;
          if (!isQuickProbe) {
            const fileEl = document.getElementById('fileInput');
            const picked = fileEl && fileEl.files ? Array.from(fileEl.files) : [];
            if (picked.length > 0) {
              const parts = [{ type: 'text', text: prompt }];
              for (const f of picked) {
                const fd = new FormData();
                fd.append('file', f, f.name);
                fd.append('purpose', 'user_data');
                const upRes = await fetch('/v1/files', { method: 'POST', headers: headers, body: fd });
                if (!upRes.ok) throw new Error('附件上传失败：' + (await upRes.text()));
                const upMeta = await upRes.json();
                if (upMeta.category === 'image') parts.push({ type: 'image_url', image_url: { url: upMeta.id } });
                else if (upMeta.category === 'audio') parts.push({ type: 'input_audio', file_id: upMeta.id, filename: upMeta.filename });
                else if (upMeta.category === 'video') parts.push({ type: 'video_url', file_id: upMeta.id, filename: upMeta.filename });
                else parts.push({ type: 'file', file: { file_id: upMeta.id, filename: upMeta.filename, mime_type: upMeta.mime_type } });
              }
              messageContent = parts;
            }
          }

          const reqHeaders = Object.assign({ 'Content-Type': 'application/json' }, headers);
          if (isQuickProbe) reqHeaders['X-Bridge-Probe'] = '1';
          const earlyPollTimer = setTimeout(refreshStatus, 280);
          const res = await fetch('/v1/chat/completions', {
            method: 'POST',
            headers: reqHeaders,
            body: JSON.stringify({ model: chosenModel, stream: true, messages: [{ role: 'user', content: messageContent }] })
          });
          clearTimeout(earlyPollTimer);
          refreshStatus();
          if (!res.ok) throw new Error('服务返回错误 (' + res.status + ')：' + (await res.text()));
          const reader = res.body.getReader();
          const decoder = new TextDecoder('utf-8');
          let buffer = '';
          while (true) {
            const step = await reader.read();
            if (step.done) break;
            buffer += decoder.decode(step.value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
              const trimmed = line.trim();
              if (!trimmed.startsWith('data:')) continue;
              const payload = trimmed.slice(5).trim();
              if (payload === '[DONE]') continue;
              try {
                const parsed = JSON.parse(payload);
                const delta = parsed.choices && parsed.choices[0] && parsed.choices[0].delta;
                if (delta && delta.content) out.textContent += delta.content;
              } catch (_) {}
            }
          }
          const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
          box.className = 'probe-result ok';
          resTitle.textContent = '✓ 测试通过 · ' + modelName + ' 回复正常';
          resMeta.textContent = '耗时 ' + elapsed + ' 秒';
          resMeta.className = 'chip ok';
          if (!out.textContent.trim()) out.textContent = '（已收到正常完成信号）';
        } catch (err) {
          box.className = 'probe-result err';
          resTitle.textContent = '✕ 测试未通过';
          resMeta.textContent = '请检查连接状态';
          resMeta.className = 'chip warn';
          out.textContent = String(err.message || err);
        } finally {
          quickBtn.disabled = false;
          customBtn.disabled = false;
          await refreshStatus();
        }
      }

      document.getElementById('modelSelect').addEventListener('change', function (ev) {
        document.getElementById('probeHeaderChip').textContent = '当前测 ' + friendlyModel(ev.target.value);
      });
      document.getElementById('quickProbeBtn').addEventListener('click', function () { executeTestRequest(true); });
      document.getElementById('sendBtn').addEventListener('click', function () { executeTestRequest(false); });
      document.getElementById('refreshBtn').addEventListener('click', function () { refreshStatus(true); });
      document.getElementById('idleSeatsToggle').addEventListener('click', function () {
        idleSeatsExpanded = !idleSeatsExpanded;
        document.getElementById('idleSeatsBody').hidden = !idleSeatsExpanded;
        document.getElementById('idleSeatsHint').textContent = idleSeatsExpanded ? '收起列表 ▴' : '展开查看 ▾';
        document.getElementById('idleSeatsToggle').setAttribute('aria-expanded', idleSeatsExpanded ? 'true' : 'false');
      });
      document.getElementById('detailCloseBtn').addEventListener('click', function () {
        selectedSlotKey = null;
        document.querySelectorAll('.seat-card[data-slot-key]').forEach(function (el) {
          el.classList.remove('selected');
          el.setAttribute('aria-pressed', 'false');
        });
        renderSeatDetailPanel();
      });
      document.getElementById('detailResetBtn').addEventListener('click', function () {
        const item = selectedSlotKey ? enrichedBySlotKey[selectedSlotKey] : null;
        if (!item || item.isWorking) return;
        const s = item.slot;
        resetSingleSeat(s.slot_key, friendlySeatLabel(s.slot_key, s.tab_id), document.getElementById('detailResetBtn'));
      });
      document.getElementById('periodTabs').addEventListener('click', function (ev) {
        const btn = ev.target && ev.target.closest('.period-tab');
        if (!btn) return;
        const p = btn.getAttribute('data-period');
        if (!p) return;
        selectedPeriod = p;
        document.getElementById('datePickerInput').value = '';
        if (lastUsageSnapshot) renderLedger(lastUsageSnapshot, lastCtxSnapshot);
      });
      document.getElementById('datePickerInput').addEventListener('change', function (ev) {
        const val = ev.target.value;
        selectedPeriod = val ? ('day:' + val) : 'all';
        if (lastUsageSnapshot) renderLedger(lastUsageSnapshot, lastCtxSnapshot);
      });
      document.getElementById('ledgerTenantSelect').addEventListener('change', function (ev) {
        selectedTenant = ev.target.value || 'all';
        if (lastUsageSnapshot) renderLedger(lastUsageSnapshot, lastCtxSnapshot);
      });
      document.getElementById('ledgerResetTenantFilterBtn').addEventListener('click', function () {
        selectedTenant = 'all';
        if (lastUsageSnapshot) renderLedger(lastUsageSnapshot, lastCtxSnapshot);
      });

      function setCreateKeyDrawer(openFlag) {
        createKeyPanelOpen = Boolean(openFlag);
        document.getElementById('createKeyPanel').hidden = !createKeyPanelOpen;
        document.getElementById('toggleCreateKeyBtn').textContent = createKeyPanelOpen ? '收起新建面板' : '+ 新建专属密钥';
      }
      document.getElementById('toggleCreateKeyBtn').addEventListener('click', function () {
        setCreateKeyDrawer(!createKeyPanelOpen);
      });
      document.getElementById('cancelCreateKeyBtn').addEventListener('click', function () {
        setCreateKeyDrawer(false);
      });
      function buildAdminHeaders() {
        const authVal = document.getElementById('apiKeyInput').value.trim();
        const h = { 'Content-Type': 'application/json' };
        if (authVal) h['Authorization'] = 'Bearer ' + authVal;
        return h;
      }

      function stripTrailingSlashes(str) {
        let s = String(str || '').trim();
        while (s.endsWith('/')) s = s.slice(0, -1);
        return s;
      }

      function resolveShareApiBase(policyObj) {
        const tunUrl = (lastTunnelSnapshot && lastTunnelSnapshot.running && lastTunnelSnapshot.active_url) ? lastTunnelSnapshot.active_url : '';
        let raw = stripTrailingSlashes((policyObj && policyObj.public_base_url) || tunUrl || window.location.origin || '');
        if (!raw.endsWith('/v1')) raw = raw + '/v1';
        return raw;
      }

      function dateInputToExpiresTs(val) {
        const s = String(val || '').trim();
        if (!s) return 0;
        const ms = Date.parse(s + 'T23:59:59+08:00');
        return isNaN(ms) ? 0 : Math.floor(ms / 1000);
      }

      function expiresTsToDateInput(ts) {
        const n = Number(ts || 0);
        if (n <= 0) return '';
        const dt = new Date((n + 8 * 3600) * 1000);
        return isNaN(dt.getTime()) ? '' : dt.toISOString().slice(0, 10);
      }

      function makeQuotaBarLine(title, usedVal, limitVal, fmtFn) {
        const wrap = document.createElement('div');
        wrap.className = 'quota-line';
        const top = document.createElement('div');
        top.className = limitVal > 0 ? 'quota-line-top' : 'quota-line-top compact';
        const lSpan = document.createElement('span');
        lSpan.textContent = limitVal > 0 ? title : (title + '：');
        const rSpan = document.createElement('span');
        rSpan.className = 'quota-line-val';
        const fmt = fmtFn || function (x) { return String(x); };
        rSpan.textContent = limitVal > 0 ? (fmt(usedVal) + ' / ' + fmt(limitVal)) : fmt(usedVal);
        top.appendChild(lSpan);
        top.appendChild(rSpan);
        if (!(limitVal > 0)) {
          const infTag = document.createElement('span');
          infTag.className = 'muted';
          infTag.textContent = '· 不限';
          top.appendChild(infTag);
        }
        wrap.appendChild(top);
        if (limitVal > 0) {
          const track = document.createElement('div');
          track.className = 'quota-bar-track';
          const fill = document.createElement('div');
          const pct = Math.min(100, Math.max(0, (usedVal / limitVal) * 100));
          fill.className = 'quota-bar-fill' + (pct >= 100 ? ' err' : (pct >= 80 ? ' warn' : ''));
          fill.style.width = pct.toFixed(1) + '%';
          track.appendChild(fill);
          wrap.appendChild(track);
        }
        return wrap;
      }

      function renderTunnel(tun, dist) {
        if (!tun) return;
        lastTunnelSnapshot = tun;
        const running = Boolean(tun.running);
        const activeUrl = stripTrailingSlashes(tun.active_url || '');
        const policyUrl = stripTrailingSlashes((dist && dist.policy && dist.policy.public_base_url) || '');
        const effOrigin = activeUrl || policyUrl;
        const cleanOrigin = effOrigin.endsWith('/v1') ? stripTrailingSlashes(effOrigin.slice(0, -3)) : effOrigin;
        const effApiUrl = effOrigin ? (effOrigin + (effOrigin.endsWith('/v1') ? '' : '/v1')) : '';
        const isPublicOrigin = Boolean(cleanOrigin) && cleanOrigin.indexOf('127.0.0.1') === -1 && cleanOrigin.indexOf('localhost') === -1;

        const chip = document.getElementById('tunnelStatusChip');
        const navChip = document.getElementById('navTunnelChip');
        const subHint = document.getElementById('tunnelSubHint');
        const urlVal = document.getElementById('tunnelUrlVal');
        const copyBtn = document.getElementById('tunnelCopyUrlBtn');
        const copyStatusBtn = document.getElementById('tunnelCopyStatusBtn');
        const openMapBtn = document.getElementById('tunnelOpenMapBtn');
        const startBtn = document.getElementById('tunnelStartQuickBtn');
        const restartBtn = document.getElementById('tunnelRestartQuickBtn');
        const stopBtn = document.getElementById('tunnelStopBtn');
        const probeBox = document.getElementById('tunnelProbeBox');
        const ovTVal = document.getElementById('ovTunnelVal');
        const ovTSub = document.getElementById('ovTunnelSub');
        const ovTBadge = document.getElementById('ovTunnelBadge');

        if (copyStatusBtn) copyStatusBtn.disabled = !cleanOrigin;
        if (openMapBtn) openMapBtn.disabled = !isPublicOrigin;

        const keyCnt = (dist && Array.isArray(dist.clients)) ? dist.clients.length : 0;
        const lp = tun.last_probe;
        const lpMatchesOrigin = Boolean(
          lp && cleanOrigin && (!lp.public_url || stripTrailingSlashes(lp.public_url) === cleanOrigin)
        );
        const lpFailed = Boolean(lpMatchesOrigin && lp.health_ok === false);
        const lpHealthy = Boolean(lpMatchesOrigin && lp.health_ok === true);

        if (running && activeUrl) {
          chip.className = lpFailed ? 'chip warn' : 'chip ok';
          chip.textContent = lpFailed ? '公网连接异常' : (tun.mode === 'named' ? '固定域名通道运行中' : '公网通道运行中');
          if (navChip) {
            navChip.className = lpFailed ? 'side-badge busy' : 'side-badge ok';
            navChip.textContent = lpFailed ? '连接异常' : '已开启';
          }
          if (ovTVal) ovTVal.textContent = lpFailed ? '公网未连通' : (lpHealthy ? '公网已连通' : '公网待自检');
          if (ovTSub) ovTSub.textContent = effOrigin.replace(/^https?:\/\//, '') + ' · ' + keyCnt + ' 把密钥';
          if (ovTBadge) {
            ovTBadge.className = lpFailed ? 'chip warn' : (lpHealthy ? 'chip ok' : 'chip');
            ovTBadge.textContent = lpFailed ? '连接异常 →' : (lpHealthy ? '在线 →' : '待自检 →');
          }
          subHint.textContent = '外部软件填入下方接口地址与专属密钥即可使用；本机管理页面对外完全隐藏隔离';
          urlVal.textContent = effApiUrl;
          copyBtn.disabled = false;
          startBtn.hidden = true;
          restartBtn.hidden = (tun.mode === 'named');
          stopBtn.hidden = false;
        } else if (running) {
          chip.className = 'chip busy';
          chip.textContent = '正在分配地址...';
          if (navChip) { navChip.className = 'side-badge busy'; navChip.textContent = '连接中'; }
          if (ovTVal) ovTVal.textContent = '正在申请地址...';
          if (ovTBadge) { ovTBadge.className = 'chip busy'; ovTBadge.textContent = '连接中 →'; }
          urlVal.textContent = '正在向 Cloudflare 申请加密公网地址，请稍候 2-3 秒...';
          copyBtn.disabled = true;
          startBtn.hidden = true;
          restartBtn.hidden = true;
          stopBtn.hidden = false;
        } else {
          chip.className = tun.cloudflared_installed ? 'chip' : 'chip warn';
          chip.textContent = tun.cloudflared_installed ? '未开启' : '未装 cloudflared';
          if (navChip) {
            navChip.className = policyUrl && lpHealthy ? 'side-badge ok' : (lpFailed ? 'side-badge busy' : 'side-badge');
            navChip.textContent = policyUrl ? (lpFailed ? '连接异常' : '外部域名') : '未开启';
          }
          if (ovTVal) ovTVal.textContent = policyUrl ? (lpFailed ? '公网未连通' : (lpHealthy ? '公网已连通' : '公网待自检')) : '仅本机使用';
          if (ovTSub) ovTSub.textContent = keyCnt > 0 ? ('已创建 ' + keyCnt + ' 把专属密钥 · 点击开启公网') : '点击一键生成免费公网接口地址';
          if (ovTBadge) {
            ovTBadge.className = lpFailed ? 'chip warn' : (policyUrl && lpHealthy ? 'chip ok' : 'chip');
            ovTBadge.textContent = lpFailed ? '连接异常 →' : (policyUrl && lpHealthy ? '在线 →' : '查看 →');
          }
          subHint.textContent = tun.last_error || '免买服务器免域名，点击右侧「一键开启免费公网」即可生成外部 HTTPS 地址';
          urlVal.textContent = effApiUrl || '尚未开启公网通道（点击右上角「一键开启免费公网」自动生成）';
          copyBtn.disabled = !effApiUrl;
          startBtn.hidden = false;
          restartBtn.hidden = true;
          stopBtn.hidden = true;
        }

        const namedUrlEl = document.getElementById('tunnelNamedUrlInput');
        if (namedUrlEl && document.activeElement !== namedUrlEl && tun.named_public_url && !namedUrlEl.value) {
          namedUrlEl.value = tun.named_public_url;
        }

        if (!tunnelActionInFlight) {
          if ((running || policyUrl) && lp && lp.detail && lpMatchesOrigin) {
            probeBox.hidden = false;
            probeBox.className = 'tunnel-probe-box ' + (lp.ok ? 'ok' : 'warn');
            const checks = [];
            checks.push('HTTPS 连通: ' + (lp.health_ok ? '✓' : '✕'));
            checks.push('免密拦截: ' + (lp.auth_lock_verified ? '✓' : '✕'));
            checks.push('总台隔离: ' + (lp.admin_lock_verified ? '✓' : '✕'));
            if (lp.client_key_verified !== null && lp.client_key_verified !== undefined) {
              checks.push('专属密钥验证: ' + (lp.client_key_verified ? '✓' : '✕'));
            }
            probeBox.textContent = lp.detail + ' ｜ ' + checks.join(' · ');
          } else if (tun.last_error && !running) {
            probeBox.hidden = false;
            probeBox.className = 'tunnel-probe-box warn';
            probeBox.textContent = tun.last_error;
          } else {
            probeBox.hidden = true;
            probeBox.textContent = '';
          }
        }
      }

      function buildClientBundleText(clientObj, policyObj, defModel) {
        const apiBase = resolveShareApiBase(policyObj);
        const baseOrigin = apiBase.endsWith('/v1') ? stripTrailingSlashes(apiBase.slice(0, -3)) : stripTrailingSlashes(apiBase);
        const statusUrl = baseOrigin + '/status';
        const keyStr = clientObj.api_key || clientObj.key_preview || '';
        const defaultCatalog = ['gemini-4-carbon', 'gemini-4-hydrogen', 'gemini-4-argon', 'gemini-3.1-pro', 'gemini-3.8-flash'];
        if (defModel && defaultCatalog.indexOf(defModel) === -1) defaultCatalog.unshift(defModel);
        if (Array.isArray(dynamicExtraModels)) {
          dynamicExtraModels.forEach(function (mid) {
            if (mid && defaultCatalog.indexOf(mid) === -1) defaultCatalog.push(mid);
          });
        }
        const modelList = (Array.isArray(clientObj.allowed_models) && clientObj.allowed_models.length > 0)
          ? clientObj.allowed_models
          : defaultCatalog;
        const clientLabel = String(clientObj.name || 'Client').trim();
        const lines = [
          '[' + clientLabel + ' · API 接入凭证 | API Credentials]',
          '接口地址 (Base URL): ' + apiBase,
          '访问密钥 (API Key): ' + keyStr,
          '可用模型 (Models): ' + modelList.join(', '),
          '状态监控 (Status Monitor): ' + statusUrl
        ];
        return lines.join('\n');
      }

      function showShareBundle(clientObj) {
        const policyObj = (lastDistSnapshot && lastDistSnapshot.policy) || {};
        currentShareKey = String(clientObj.api_key || '');
        currentShareBundle = buildClientBundleText(clientObj, policyObj, lastDefaultModel);
        document.getElementById('distShareTitle').textContent = (clientObj.name || 'Client') + ' · API 接入凭证 (API Credentials)';
        document.getElementById('distSharePre').textContent = currentShareBundle;
        document.getElementById('distShareBox').hidden = false;
      }

      async function copyTextWithFeedback(btn, text, okLabel) {
        const orig = btn.textContent;
        try {
          await navigator.clipboard.writeText(text);
          btn.textContent = okLabel || '已复制 ✓';
        } catch (_) {
          btn.textContent = '请手动复制';
        }
        setTimeout(function () { btn.textContent = orig; }, 1400);
      }

      function renderDistribution(dist, defModel) {
        if (!dist) return;
        lastDistSnapshot = dist;
        const policy = dist.policy || {};
        const rt = dist.runtime || {};
        const clientsList = Array.isArray(dist.clients) ? dist.clients : [];
        const enabled = policy.external_access_enabled !== false;
        const reserved = Number(policy.host_reserved_slots !== undefined ? policy.host_reserved_slots : 2);
        const poolCap = Number(rt.pool_capacity || 0);
        const extCap = Number(rt.external_parallel_cap || 0);
        const extInf = Number(rt.external_in_flight || 0);
        const extQueued = Number(rt.external_queued || 0);
        const hostInf = Number(rt.host_in_flight || 0);
        const activeKeys = Number(rt.active_client_keys || 0);
        const totalKeys = Number(rt.total_client_keys || clientsList.length);

        if (!createKeyAutoInitialized) {
          createKeyAutoInitialized = true;
          if (clientsList.length === 0) setCreateKeyDrawer(true);
        }

        const sumChip = document.getElementById('distSummaryChip');
        if (!enabled) {
          sumChip.className = 'chip warn';
          sumChip.textContent = '外部已暂停 · ' + totalKeys + ' 把密钥';
        } else if (extInf > 0 || extQueued > 0) {
          sumChip.className = 'chip busy';
          sumChip.textContent = '外部 ' + extInf + ' 工作中' + (extQueued > 0 ? (' / ' + extQueued + ' 排队') : '') + ' · ' + activeKeys + ' 把密钥';
        } else {
          sumChip.className = activeKeys > 0 ? 'chip ok' : 'chip';
          sumChip.textContent = '本机保底 ' + reserved + ' 席 · ' + activeKeys + ' 把密钥启用';
        }

        const badge = document.getElementById('distAccessBadge');
        badge.className = enabled ? 'chip ok' : 'chip warn';
        badge.textContent = enabled ? '外部共享已开启' : '外部共享已暂停';

        const splitEl = document.getElementById('distPoolSplitText');
        if (poolCap > 0) {
          splitEl.textContent = '当前在线 ' + poolCap + ' 席 = 本机保底 ' + Math.min(poolCap, reserved) + ' 席 + 外部最多并发 ' + extCap + ' 席（本机 ' + hostInf + ' / 外部 ' + extInf + ' 工作中' + (extQueued > 0 ? ('，排队 ' + extQueued) : '') + '）';
        } else {
          splitEl.textContent = '本机直连免密且优先使用席位；外部客户端按并发上限与公平队列自动调度';
        }

        const policyInputs = [
          document.getElementById('distEnabledSelect'),
          document.getElementById('distReservedInput'),
          document.getElementById('distQueueTimeoutInput'),
          document.getElementById('distPublicUrlInput')
        ];
        const anyPolicyFocused = policyInputs.some(function (el) { return el && document.activeElement === el; });
        if (!distPolicyDirty && !anyPolicyFocused) {
          document.getElementById('distEnabledSelect').value = enabled ? '1' : '0';
          document.getElementById('distReservedInput').value = String(reserved);
          document.getElementById('distQueueTimeoutInput').value = String(Math.round(Number(policy.external_queue_timeout_seconds || 90)));
          document.getElementById('distPublicUrlInput').value = String(policy.public_base_url || '');
        }

        const tbody = document.getElementById('distClientsBody');
        if (clientsList.length === 0) {
          renderEmptyRow(tbody, 6, '暂未创建外部密钥 · 如需分享给他人请在上方填写名称并生成专属密钥');
          return;
        }
        tbody.replaceChildren();
        const nowMs = Date.now();
        clientsList.forEach(function (c) {
          const tr = document.createElement('tr');
          const tid = c.tenant_id || ('client:' + c.client_id);

          const nameBox = document.createElement('div');
          const nameRow = document.createElement('div');
          nameRow.style.display = 'flex';
          nameRow.style.alignItems = 'center';
          nameRow.style.gap = '6px';
          nameRow.style.flexWrap = 'wrap';
          const strong = document.createElement('strong');
          strong.textContent = c.name || c.client_id;
          const st = document.createElement('span');
          if (c.expired) { st.className = 'chip err'; st.textContent = '已到期'; }
          else if (c.enabled) { st.className = 'chip ok'; st.textContent = '启用中'; }
          else { st.className = 'chip warn'; st.textContent = '已暂停'; }
          nameRow.appendChild(strong);
          nameRow.appendChild(st);
          nameBox.appendChild(nameRow);
          if (c.note) {
            const noteSub = document.createElement('div');
            noteSub.className = 'muted';
            noteSub.style.fontSize = '0.75rem';
            noteSub.textContent = c.note;
            nameBox.appendChild(noteSub);
          }
          const ruleTags = [];
          if (Array.isArray(c.allowed_models) && c.allowed_models.length > 0) ruleTags.push('模型: ' + c.allowed_models.map(friendlyModel).join('/'));
          if (Array.isArray(c.allowed_ips) && c.allowed_ips.length > 0) ruleTags.push('白名单 ' + c.allowed_ips.length + ' 条');
          if (Number(c.expires_at || 0) > 0) ruleTags.push('至 ' + expiresTsToDateInput(c.expires_at));
          if (ruleTags.length > 0) {
            const ruleDiv = document.createElement('div');
            ruleDiv.className = 'muted';
            ruleDiv.style.fontSize = '0.73rem';
            ruleDiv.style.marginTop = '2px';
            ruleDiv.textContent = ruleTags.join(' · ');
            nameBox.appendChild(ruleDiv);
          }
          tr.appendChild(makeCell(nameBox));

          const keyBox = document.createElement('div');
          keyBox.className = 'cell-two-line';
          const codeSpan = document.createElement('div');
          codeSpan.className = 'mono line-main';
          codeSpan.style.fontSize = '0.8rem';
          codeSpan.style.fontWeight = '600';
          codeSpan.textContent = c.key_preview || 'sk-geb-****';
          keyBox.appendChild(codeSpan);
          if (c.api_key) {
            const copyWrap = document.createElement('div');
            copyWrap.className = 'dist-row-actions';
            copyWrap.style.flexWrap = 'nowrap';
            const cpBundleBtn = document.createElement('button');
            cpBundleBtn.type = 'button';
            cpBundleBtn.className = 'btn xs soft';
            cpBundleBtn.textContent = '复制接入包';
            cpBundleBtn.addEventListener('click', function () {
              showShareBundle(c);
              copyTextWithFeedback(cpBundleBtn, buildClientBundleText(c, policy, defModel), '已复制 ✓');
            });
            const cpKeyBtn = document.createElement('button');
            cpKeyBtn.type = 'button';
            cpKeyBtn.className = 'btn xs';
            cpKeyBtn.textContent = '复制密钥';
            cpKeyBtn.addEventListener('click', function () {
              copyTextWithFeedback(cpKeyBtn, c.api_key, '已复制 ✓');
            });
            copyWrap.appendChild(cpBundleBtn);
            copyWrap.appendChild(cpKeyBtn);
            keyBox.appendChild(copyWrap);
          }
          tr.appendChild(makeCell(keyBox, 'nw'));

          const inf = Number(c.in_flight || 0);
          const qd = Number(c.queued || 0);
          const maxC = Number(c.max_concurrency || 1);
          const concWrap = document.createElement('div');
          concWrap.className = 'cell-two-line';
          const concChip = document.createElement('span');
          concChip.className = inf > 0 ? 'chip busy' : 'chip';
          concChip.textContent = inf + ' / ' + maxC + ' 并发' + (qd > 0 ? ('（排 ' + qd + '）') : '');
          concWrap.appendChild(concChip);
          const rpmSub = document.createElement('div');
          rpmSub.className = 'line-sub';
          rpmSub.textContent = '限速 ' + (c.rpm_limit || 30) + ' 次/分';
          concWrap.appendChild(rpmSub);
          tr.appendChild(makeCell(concWrap, 'nw'));

          const reqLim = Number(c.daily_request_limit || 0);
          const tokLim = Number(c.daily_token_limit || 0);
          const costLim = Number(c.daily_cost_limit_usd || 0);
          const totTokLim = Number(c.total_token_limit || 0);
          const todayReqs = Number(c.today_requests || 0);
          const todayToks = Number(c.today_tokens || 0);
          const todayCostUsd = Number(c.today_cost_usd || 0);
          const totToks = Number(c.total_tokens || 0);
          const hasAnyQuotaLimit = (reqLim > 0 || tokLim > 0 || costLim > 0 || totTokLim > 0);
          let quotaCellNode;
          if (!hasAnyQuotaLimit) {
            const unlimBox = document.createElement('div');
            unlimBox.className = 'cell-two-line';
            const unlimChip = document.createElement('span');
            unlimChip.className = 'chip';
            unlimChip.textContent = '不限额度';
            const unlimSub = document.createElement('div');
            unlimSub.className = 'line-sub';
            unlimSub.textContent = todayToks > 0
              ? ('今日 ' + todayReqs + ' 次 · ' + fmtLedgerTok(todayToks))
              : ('今日已用 ' + todayReqs + ' 次');
            unlimBox.appendChild(unlimChip);
            unlimBox.appendChild(unlimSub);
            quotaCellNode = unlimBox;
          } else {
            const qStack = document.createElement('div');
            qStack.className = 'quota-stack';
            if (reqLim > 0 || todayReqs > 0) qStack.appendChild(makeQuotaBarLine('今日次数', todayReqs, reqLim, function (v) { return v + ' 次'; }));
            if (tokLim > 0 || todayToks > 0) qStack.appendChild(makeQuotaBarLine('今日 Token', todayToks, tokLim, fmtLedgerTok));
            if (costLim > 0) qStack.appendChild(makeQuotaBarLine('今日预算', todayCostUsd, costLim, function (v) { return '$' + Number(v || 0).toFixed(2); }));
            if (totTokLim > 0) qStack.appendChild(makeQuotaBarLine('总封顶', totToks, totTokLim, fmtLedgerTok));
            quotaCellNode = qStack;
          }
          tr.appendChild(makeCell(quotaCellNode, 'nw'));

          const totReqs = Number(c.total_requests || 0);
          const totCostUsd = Number(c.total_cost_usd || 0);
          const fxRate = Number((lastUsageSnapshot && lastUsageSnapshot.usd_to_cny_rate) || 6.8);
          const totErrs = Number(c.total_errors || 0);
          const lastStr = c.last_used_at ? new Date(c.last_used_at * 1000).toLocaleTimeString() : '尚未使用';
          const statBox = document.createElement('div');
          statBox.className = 'cell-two-line';
          const statTop = document.createElement('div');
          statTop.className = 'line-main';
          statTop.textContent = totReqs + ' 次 · ' + fmtMoney(totCostUsd, totCostUsd * fxRate);
          const statSub = document.createElement('div');
          statSub.className = 'line-sub';
          statSub.textContent = lastStr + (totErrs > 0 ? (' · 拦截 ' + totErrs) : '');
          statBox.appendChild(statTop);
          statBox.appendChild(statSub);
          tr.appendChild(makeCell(statBox, 'nw'));

          const actBox = document.createElement('div');
          actBox.className = 'dist-row-actions';
          actBox.style.flexWrap = 'nowrap';
          const editBtn = document.createElement('button');
          editBtn.type = 'button';
          editBtn.className = 'btn sm soft';
          editBtn.textContent = '编辑限额';
          editBtn.addEventListener('click', function () { openClientEditModal(c); });

          const viewLedgerBtn = document.createElement('button');
          viewLedgerBtn.type = 'button';
          viewLedgerBtn.className = 'btn sm';
          viewLedgerBtn.textContent = '看账本';
          viewLedgerBtn.addEventListener('click', function () {
            selectedTenant = tid;
            if (lastUsageSnapshot) renderLedger(lastUsageSnapshot, lastCtxSnapshot);
            jumpToSection('secLedger');
          });

          // Clean Dropdown Menu for Low-Frequency / Destructive Actions
          const moreDetails = document.createElement('details');
          moreDetails.className = 'row-more-menu';
          const moreSum = document.createElement('summary');
          moreSum.textContent = '更多 ▾';
          moreDetails.appendChild(moreSum);

          const pop = document.createElement('div');
          pop.className = 'row-more-pop';
          function syncMorePopPos() {
            const rect = moreSum.getBoundingClientRect();
            if (!rect || (rect.width === 0 && rect.height === 0)) return;
            const rightGap = Math.max(12, window.innerWidth - rect.right);
            pop.style.right = rightGap + 'px';
            if (rect.bottom + 170 > window.innerHeight && rect.top > 180) {
              pop.style.top = 'auto';
              pop.style.bottom = (window.innerHeight - rect.top + 5) + 'px';
            } else {
              pop.style.bottom = 'auto';
              pop.style.top = (rect.bottom + 5) + 'px';
            }
          }
          if (openClientMenuId === c.client_id) {
            moreDetails.open = true;
            requestAnimationFrame(syncMorePopPos);
          }
          moreDetails.addEventListener('toggle', function () {
            if (moreDetails.open) {
              openClientMenuId = c.client_id;
              syncMorePopPos();
            } else if (openClientMenuId === c.client_id) {
              openClientMenuId = '';
            }
          });

          const toggleBtn = document.createElement('button');
          toggleBtn.type = 'button';
          toggleBtn.className = 'btn sm';
          toggleBtn.textContent = c.enabled ? '⏸ 暂停使用' : '▶ 恢复启用';
          toggleBtn.addEventListener('click', async function () {
            toggleBtn.disabled = true;
            try {
              const res = await fetch('/api/distribution/clients/' + encodeURIComponent(c.client_id), {
                method: 'PATCH', headers: buildAdminHeaders(), body: JSON.stringify({ enabled: !c.enabled })
              });
              const d = await res.json().catch(function () { return {}; });
              openClientMenuId = '';
              if (res.ok && d.distribution) renderDistribution(d.distribution, lastDefaultModel);
            } finally { toggleBtn.disabled = false; }
          });

          const discBtn = document.createElement('button');
          discBtn.type = 'button';
          discBtn.className = 'btn sm';
          discBtn.textContent = '⚡ 断连清场';
          discBtn.addEventListener('click', async function () {
            discBtn.disabled = true;
            try {
              const res = await fetch('/api/distribution/clients/' + encodeURIComponent(c.client_id) + '/disconnect', {
                method: 'POST', headers: buildAdminHeaders(), body: JSON.stringify({ purge_archive: false })
              });
              const d = await res.json().catch(function () { return {}; });
              openClientMenuId = '';
              if (res.ok && d.distribution) { renderDistribution(d.distribution, lastDefaultModel); refreshStatus(); }
            } finally { discBtn.disabled = false; }
          });

          const rotKey = 'rot:' + c.client_id;
          const rotPending = Number(pendingConfirmMap[rotKey] || 0) > nowMs;
          const rotBtn = document.createElement('button');
          rotBtn.type = 'button';
          rotBtn.className = 'btn sm';
          rotBtn.textContent = rotPending ? '确认换新密钥?' : '↻ 更换密钥';
          rotBtn.addEventListener('click', async function () {
            if (!(Number(pendingConfirmMap[rotKey] || 0) > Date.now())) {
              pendingConfirmMap[rotKey] = Date.now() + 4000;
              rotBtn.textContent = '确认换新密钥?';
              return;
            }
            delete pendingConfirmMap[rotKey];
            rotBtn.disabled = true;
            try {
              const res = await fetch('/api/distribution/clients/' + encodeURIComponent(c.client_id) + '/rotate-key', {
                method: 'POST', headers: buildAdminHeaders()
              });
              const d = await res.json().catch(function () { return {}; });
              openClientMenuId = '';
              if (res.ok && d.distribution) {
                renderDistribution(d.distribution, lastDefaultModel);
                if (d.client) showShareBundle(d.client);
              }
            } finally { rotBtn.disabled = false; }
          });

          const delKey = 'del:' + c.client_id;
          const delPending = Number(pendingConfirmMap[delKey] || 0) > nowMs;
          const delBtn = document.createElement('button');
          delBtn.type = 'button';
          delBtn.className = 'btn sm danger-soft';
          delBtn.textContent = delPending ? '确认吊销删除?' : '✕ 吊销密钥';
          delBtn.addEventListener('click', async function () {
            if (!(Number(pendingConfirmMap[delKey] || 0) > Date.now())) {
              pendingConfirmMap[delKey] = Date.now() + 4000;
              delBtn.textContent = '确认吊销删除?';
              return;
            }
            delete pendingConfirmMap[delKey];
            delBtn.disabled = true;
            try {
              const res = await fetch('/api/distribution/clients/' + encodeURIComponent(c.client_id), {
                method: 'DELETE', headers: buildAdminHeaders()
              });
              const d = await res.json().catch(function () { return {}; });
              openClientMenuId = '';
              if (res.ok && d.distribution) {
                renderDistribution(d.distribution, lastDefaultModel);
                document.getElementById('distShareBox').hidden = true;
                refreshStatus();
              }
            } finally { delBtn.disabled = false; }
          });

          pop.appendChild(toggleBtn);
          pop.appendChild(discBtn);
          pop.appendChild(rotBtn);
          pop.appendChild(delBtn);
          moreDetails.appendChild(pop);

          actBox.appendChild(editBtn);
          actBox.appendChild(viewLedgerBtn);
          actBox.appendChild(moreDetails);
          tr.appendChild(makeCell(actBox));
          tbody.appendChild(tr);
        });
      }
      ['distEnabledSelect', 'distReservedInput', 'distQueueTimeoutInput', 'distPublicUrlInput'].forEach(function (id) {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('input', function () { distPolicyDirty = true; });
        el.addEventListener('change', function () { distPolicyDirty = true; });
      });

      document.getElementById('distSavePolicyBtn').addEventListener('click', async function () {
        const btn = document.getElementById('distSavePolicyBtn');
        const msg = document.getElementById('distPolicyMsg');
        btn.disabled = true;
        try {
          const payload = {
            external_access_enabled: document.getElementById('distEnabledSelect').value === '1',
            host_reserved_slots: Math.max(0, Number(document.getElementById('distReservedInput').value || 0)),
            external_queue_timeout_seconds: Math.max(5, Number(document.getElementById('distQueueTimeoutInput').value || 90)),
            public_base_url: document.getElementById('distPublicUrlInput').value.trim()
          };
          const res = await fetch('/api/distribution/policy', {
            method: 'PATCH', headers: buildAdminHeaders(), body: JSON.stringify(payload)
          });
          const d = await res.json().catch(function () { return {}; });
          if (!res.ok || !d.ok) {
            msg.textContent = (d && d.detail) ? d.detail : '保存失败';
            return;
          }
          distPolicyDirty = false;
          if (d.distribution) renderDistribution(d.distribution, lastDefaultModel);
          msg.textContent = '已保存生效 ✓';
          setTimeout(function () { msg.textContent = ''; }, 2000);
        } catch (_) {
          msg.textContent = '网络异常';
        } finally {
          btn.disabled = false;
        }
      });

      document.getElementById('distCreateClientBtn').addEventListener('click', async function () {
        const btn = document.getElementById('distCreateClientBtn');
        const nameEl = document.getElementById('distClientName');
        const nameVal = nameEl.value.trim();
        if (!nameVal) {
          nameEl.focus();
          const orig = btn.textContent;
          btn.textContent = '请先填写名称';
          setTimeout(function () { btn.textContent = orig; }, 1400);
          return;
        }
        btn.disabled = true;
        try {
          const payload = {
            name: nameVal,
            max_concurrency: Math.max(1, Number(document.getElementById('distClientConcurrency').value || 2)),
            rpm_limit: Math.max(1, Number(document.getElementById('distClientRpm').value || 30)),
            daily_request_limit: Math.max(0, Number(document.getElementById('distClientDailyReq').value || 0)),
            daily_token_limit: Math.max(0, Number(document.getElementById('distClientDailyTok').value || 0)),
            daily_cost_limit_usd: Math.max(0, Number(document.getElementById('distClientDailyCost').value || 0)),
            total_token_limit: Math.max(0, Number(document.getElementById('distClientTotalTok').value || 0)),
            expires_at: dateInputToExpiresTs(document.getElementById('distClientExpiresDate').value),
            allowed_models: document.getElementById('distClientAllowedModels').value.trim(),
            allowed_ips: document.getElementById('distClientAllowedIps').value.trim(),
            note: document.getElementById('distClientNote').value.trim()
          };
          const res = await fetch('/api/distribution/clients', {
            method: 'POST', headers: buildAdminHeaders(), body: JSON.stringify(payload)
          });
          const d = await res.json().catch(function () { return {}; });
          if (res.ok && d.ok) {
            nameEl.value = '';
            document.getElementById('distClientNote').value = '';
            document.getElementById('distClientExpiresDate').value = '';
            document.getElementById('distClientAllowedModels').value = '';
            document.getElementById('distClientAllowedIps').value = '';
            setCreateKeyDrawer(false);
            if (d.distribution) renderDistribution(d.distribution, lastDefaultModel);
            if (d.client) showShareBundle(d.client);
          }
        } finally {
          btn.disabled = false;
        }
      });

      async function handleTunnelStart(mode, restartFlag) {
        const startBtn = document.getElementById('tunnelStartQuickBtn');
        const restartBtn = document.getElementById('tunnelRestartQuickBtn');
        const namedBtn = document.getElementById('tunnelStartNamedBtn');
        const probeBox = document.getElementById('tunnelProbeBox');
        tunnelActionInFlight = true;
        startBtn.disabled = true;
        restartBtn.disabled = true;
        namedBtn.disabled = true;
        probeBox.hidden = false;
        probeBox.className = 'tunnel-probe-box';
        probeBox.textContent = '正在申请 Cloudflare 加密公网通道并等待全球 DNS 生效，大约需要 4-8 秒...';
        try {
          const body = { mode: mode, restart: Boolean(restartFlag), auto_sync_public_url: true };
          if (mode === 'named') {
            body.named_tunnel_token = document.getElementById('tunnelNamedTokenInput').value.trim();
            body.named_public_url = document.getElementById('tunnelNamedUrlInput').value.trim();
          }
          const res = await fetch('/api/tunnel/start', {
            method: 'POST', headers: buildAdminHeaders(), body: JSON.stringify(body)
          });
          const d = await res.json().catch(function () { return {}; });
          if (!res.ok || !d.ok) {
            tunnelActionInFlight = false;
            probeBox.className = 'tunnel-probe-box warn';
            probeBox.textContent = (d && d.detail) ? d.detail : '开启公网通道失败';
            if (d && d.tunnel) renderTunnel(d.tunnel, lastDistSnapshot);
            return;
          }
          distPolicyDirty = false;
          if (d.distribution) renderDistribution(d.distribution, lastDefaultModel);
          if (d.tunnel) renderTunnel(d.tunnel, d.distribution || lastDistSnapshot);
          probeBox.hidden = false;
          probeBox.className = 'tunnel-probe-box';
          probeBox.textContent = '公网地址已分配，正在自动核验 HTTPS 连通、免密拦截与管理台隔离...';
          const prRes = await fetch('/api/tunnel/probe', {
            method: 'POST', headers: buildAdminHeaders(), body: JSON.stringify({})
          });
          const prData = await prRes.json().catch(function () { return {}; });
          tunnelActionInFlight = false;
          if (prRes.ok && prData && prData.tunnel) {
            if (prData.distribution) renderDistribution(prData.distribution, lastDefaultModel);
            renderTunnel(prData.tunnel, prData.distribution || d.distribution || lastDistSnapshot);
          } else {
            probeBox.className = 'tunnel-probe-box warn';
            probeBox.textContent = '公网通道已申请，但安全自检未完成：' + ((prData && prData.detail) || '请重新点击「安全自检」确认公网是否可用');
          }
        } catch (e) {
          tunnelActionInFlight = false;
          probeBox.className = 'tunnel-probe-box warn';
          probeBox.textContent = '开启异常：' + String(e.message || e);
        } finally {
          tunnelActionInFlight = false;
          startBtn.disabled = false;
          restartBtn.disabled = false;
          namedBtn.disabled = false;
        }
      }

      document.getElementById('tunnelStartQuickBtn').addEventListener('click', function () { handleTunnelStart('quick', false); });
      document.getElementById('tunnelStartNamedBtn').addEventListener('click', function () { handleTunnelStart('named', true); });
      async function executeTunnelStop() {
        const btn = document.getElementById('tunnelStopBtn');
        tunnelActionInFlight = true;
        btn.disabled = true;
        try {
          const res = await fetch('/api/tunnel/stop', {
            method: 'POST', headers: buildAdminHeaders(), body: JSON.stringify({ clear_public_url: true })
          });
          const d = await res.json().catch(function () { return {}; });
          tunnelActionInFlight = false;
          distPolicyDirty = false;
          if (d.distribution) renderDistribution(d.distribution, lastDefaultModel);
          if (d.tunnel) renderTunnel(d.tunnel, d.distribution || lastDistSnapshot);
          document.getElementById('tunnelProbeBox').hidden = true;
        } finally {
          tunnelActionInFlight = false;
          btn.disabled = false;
        }
      }

      const tunnelConfirmModal = document.getElementById('tunnelConfirmModal');
      let pendingTunnelConfirmAction = '';
      function closeTunnelConfirmModal() {
        pendingTunnelConfirmAction = '';
        tunnelConfirmModal.hidden = true;
      }
      function openTunnelConfirmModal(action) {
        if (tunnelActionInFlight) return;
        pendingTunnelConfirmAction = action;
        const titleEl = document.getElementById('tunnelConfirmTitle');
        const warnEl = document.getElementById('tunnelConfirmWarnBox');
        const urlRowEl = document.getElementById('tunnelConfirmUrlRow');
        const urlLabelEl = document.getElementById('tunnelConfirmUrlLabel');
        const urlValEl = document.getElementById('tunnelConfirmUrlVal');
        const descEl = document.getElementById('tunnelConfirmDesc');
        const okBtn = document.getElementById('confirmTunnelActionBtn');

        const activeUrl = stripTrailingSlashes((lastTunnelSnapshot && lastTunnelSnapshot.active_url) || '');
        const policyUrl = stripTrailingSlashes((lastDistSnapshot && lastDistSnapshot.policy && lastDistSnapshot.policy.public_base_url) || '');
        const effOrigin = activeUrl || policyUrl;
        const effApiUrl = effOrigin ? (effOrigin + (effOrigin.endsWith('/v1') ? '' : '/v1')) : '';
        if (effApiUrl && effApiUrl.indexOf('127.0.0.1') === -1 && effApiUrl.indexOf('localhost') === -1) {
          urlRowEl.hidden = false;
          urlValEl.textContent = effApiUrl;
        } else {
          urlRowEl.hidden = true;
        }

        if (action === 'restart') {
          titleEl.textContent = '确认更换新的公网地址？';
          warnEl.textContent = '⚠ 此操作不可逆：更换后当前公网地址将立即永久作废，无法找回或恢复原地址！';
          urlLabelEl.textContent = '即将永久作废的当前公网地址';
          descEl.textContent = '正在运行的外部请求可能会被中断；所有已填写旧地址的外部客户端与协作者将立即失联，必须重新分发并配置新生成的接口地址后才能继续调用。';
          okBtn.textContent = '确认作废旧地址并换新';
        } else {
          const isNamed = Boolean(lastTunnelSnapshot && lastTunnelSnapshot.mode === 'named');
          titleEl.textContent = '确认关闭公网加密通道？';
          warnEl.textContent = isNamed
            ? '⚠ 此操作不可逆：公网通道将立即强制断开，所有正在进行的外部请求会立刻中断且无法恢复！'
            : '⚠ 此操作不可逆：关闭后公网通道将立即断开，当前临时随机公网地址将被永久销毁，无法恢复原地址！';
          urlLabelEl.textContent = isNamed ? '即将断开的当前公网地址' : '即将永久销毁的当前公网地址';
          descEl.textContent = '关闭后外部客户端将立即无法通过公网访问本机服务（本机 127.0.0.1 直连不受影响）。若后续重新开启免费公网，系统会分配全新的随机地址，需重新通知外部使用者更新配置。';
          okBtn.textContent = '确认关闭公网';
        }
        tunnelConfirmModal.hidden = false;
      }

      document.getElementById('tunnelRestartQuickBtn').addEventListener('click', function () {
        openTunnelConfirmModal('restart');
      });
      document.getElementById('tunnelStopBtn').addEventListener('click', function () {
        openTunnelConfirmModal('stop');
      });
      document.getElementById('closeTunnelConfirmBtn').addEventListener('click', closeTunnelConfirmModal);
      document.getElementById('cancelTunnelConfirmBtn').addEventListener('click', closeTunnelConfirmModal);
      tunnelConfirmModal.addEventListener('click', function (ev) {
        if (ev.target === tunnelConfirmModal) closeTunnelConfirmModal();
      });
      document.getElementById('confirmTunnelActionBtn').addEventListener('click', function () {
        const act = pendingTunnelConfirmAction;
        closeTunnelConfirmModal();
        if (act === 'restart') handleTunnelStart('quick', true);
        else if (act === 'stop') executeTunnelStop();
      });

      document.getElementById('tunnelProbeBtn').addEventListener('click', async function () {
        const btn = document.getElementById('tunnelProbeBtn');
        const probeBox = document.getElementById('tunnelProbeBox');
        tunnelActionInFlight = true;
        btn.disabled = true;
        probeBox.hidden = false;
        probeBox.className = 'tunnel-probe-box';
        probeBox.textContent = '正在从外部视角检测公网 HTTPS 连通、免密拦截与管理台隔离保护...';
        try {
          const res = await fetch('/api/tunnel/probe', {
            method: 'POST', headers: buildAdminHeaders(), body: JSON.stringify({})
          });
          const d = await res.json().catch(function () { return {}; });
          tunnelActionInFlight = false;
          if (!res.ok) {
            probeBox.className = 'tunnel-probe-box warn';
            probeBox.textContent = (d && d.detail) ? d.detail : '公网自检未通过';
            return;
          }
          if (d.distribution) renderDistribution(d.distribution, lastDefaultModel);
          if (d.tunnel) renderTunnel(d.tunnel, d.distribution || lastDistSnapshot);
        } catch (e) {
          tunnelActionInFlight = false;
          probeBox.className = 'tunnel-probe-box warn';
          probeBox.textContent = '自检请求失败：' + String(e.message || e);
        } finally {
          tunnelActionInFlight = false;
          btn.disabled = false;
        }
      });
      document.getElementById('tunnelCopyUrlBtn').addEventListener('click', function () {
        const txt = document.getElementById('tunnelUrlVal').textContent.trim();
        if (!txt || txt.indexOf('http') !== 0) return;
        copyTextWithFeedback(document.getElementById('tunnelCopyUrlBtn'), txt, '已复制 ✓');
      });
      function resolveCurrentPublicOrigin() {
        const tunUrl = (lastTunnelSnapshot && lastTunnelSnapshot.running && lastTunnelSnapshot.active_url) ? stripTrailingSlashes(lastTunnelSnapshot.active_url) : '';
        const polUrl = stripTrailingSlashes((lastDistSnapshot && lastDistSnapshot.policy && lastDistSnapshot.policy.public_base_url) || '');
        const raw = tunUrl || polUrl || stripTrailingSlashes(window.location.origin || '');
        return raw.endsWith('/v1') ? stripTrailingSlashes(raw.slice(0, -3)) : raw;
      }
      document.getElementById('tunnelOpenStatusBtn').addEventListener('click', function () {
        const orig = resolveCurrentPublicOrigin();
        if (!orig) return;
        window.open(orig + '/status', '_blank', 'noopener,noreferrer');
      });
      document.getElementById('tunnelCopyStatusBtn').addEventListener('click', function () {
        const orig = resolveCurrentPublicOrigin();
        if (!orig || orig.indexOf('http') !== 0) return;
        copyTextWithFeedback(document.getElementById('tunnelCopyStatusBtn'), orig + '/status', '已复制 ✓');
      });
      document.getElementById('tunnelOpenMapBtn').addEventListener('click', function () {
        const orig = resolveCurrentPublicOrigin();
        if (!orig || orig.indexOf('http') !== 0) return;
        const mapUrl = orig + '/status#network-map';
        window.open(mapUrl, '_blank', 'noopener,noreferrer');
      });

      const clientEditModal = document.getElementById('clientEditModal');
      function openClientEditModal(c) {
        editingClientId = c.client_id;
        document.getElementById('editClientId').value = c.client_id;
        document.getElementById('editClientName').value = c.name || '';
        document.getElementById('editClientNote').value = c.note || '';
        document.getElementById('editClientConcurrency').value = String(c.max_concurrency || 2);
        document.getElementById('editClientRpm').value = String(c.rpm_limit || 30);
        document.getElementById('editClientDailyReq').value = String(c.daily_request_limit || 0);
        document.getElementById('editClientDailyTok').value = String(c.daily_token_limit || 0);
        document.getElementById('editClientDailyCost').value = String(c.daily_cost_limit_usd || 0);
        document.getElementById('editClientTotalTok').value = String(c.total_token_limit || 0);
        document.getElementById('editClientExpiresDate').value = expiresTsToDateInput(c.expires_at);
        document.getElementById('editClientAllowedModels').value = Array.isArray(c.allowed_models) ? c.allowed_models.join(', ') : '';
        document.getElementById('editClientAllowedIps').value = Array.isArray(c.allowed_ips) ? c.allowed_ips.join(', ') : '';
        document.getElementById('editClientMsg').textContent = '保存后立即对该密钥的新请求生效';
        clientEditModal.hidden = false;
      }

      document.getElementById('closeClientEditBtn').addEventListener('click', function () { clientEditModal.hidden = true; });
      clientEditModal.addEventListener('click', function (ev) { if (ev.target === clientEditModal) clientEditModal.hidden = true; });

      document.getElementById('saveClientEditBtn').addEventListener('click', async function () {
        if (!editingClientId) return;
        const btn = document.getElementById('saveClientEditBtn');
        const msg = document.getElementById('editClientMsg');
        btn.disabled = true;
        try {
          const payload = {
            name: document.getElementById('editClientName').value.trim(),
            note: document.getElementById('editClientNote').value.trim(),
            max_concurrency: Math.max(1, Number(document.getElementById('editClientConcurrency').value || 1)),
            rpm_limit: Math.max(1, Number(document.getElementById('editClientRpm').value || 30)),
            daily_request_limit: Math.max(0, Number(document.getElementById('editClientDailyReq').value || 0)),
            daily_token_limit: Math.max(0, Number(document.getElementById('editClientDailyTok').value || 0)),
            daily_cost_limit_usd: Math.max(0, Number(document.getElementById('editClientDailyCost').value || 0)),
            total_token_limit: Math.max(0, Number(document.getElementById('editClientTotalTok').value || 0)),
            expires_at: dateInputToExpiresTs(document.getElementById('editClientExpiresDate').value),
            allowed_models: document.getElementById('editClientAllowedModels').value.trim(),
            allowed_ips: document.getElementById('editClientAllowedIps').value.trim()
          };
          const res = await fetch('/api/distribution/clients/' + encodeURIComponent(editingClientId), {
            method: 'PATCH', headers: buildAdminHeaders(), body: JSON.stringify(payload)
          });
          const d = await res.json().catch(function () { return {}; });
          if (!res.ok || !d.ok) {
            msg.textContent = (d && d.detail) ? d.detail : '保存失败';
            return;
          }
          if (d.distribution) renderDistribution(d.distribution, lastDefaultModel);
          clientEditModal.hidden = true;
        } finally { btn.disabled = false; }
      });

      document.getElementById('editResetTodayQuotaBtn').addEventListener('click', async function () {
        if (!editingClientId) return;
        const btn = document.getElementById('editResetTodayQuotaBtn');
        const msg = document.getElementById('editClientMsg');
        btn.disabled = true;
        try {
          const res = await fetch('/api/distribution/clients/' + encodeURIComponent(editingClientId) + '/reset-quota', {
            method: 'POST', headers: buildAdminHeaders(), body: JSON.stringify({ reset_total: false })
          });
          const d = await res.json().catch(function () { return {}; });
          if (res.ok && d.distribution) {
            renderDistribution(d.distribution, lastDefaultModel);
            msg.textContent = '✓ 今日已用次数、Token 与预算计数已清零';
          }
        } finally { btn.disabled = false; }
      });

      document.getElementById('distCopyBundleBtn').addEventListener('click', function () {
        if (!currentShareBundle) return;
        copyTextWithFeedback(document.getElementById('distCopyBundleBtn'), currentShareBundle, '已复制接入包 ✓');
      });
      document.getElementById('distCopyKeyOnlyBtn').addEventListener('click', function () {
        if (!currentShareKey) return;
        copyTextWithFeedback(document.getElementById('distCopyKeyOnlyBtn'), currentShareKey, '已复制密钥 ✓');
      });
      document.getElementById('distCloseShareBtn').addEventListener('click', function () {
        document.getElementById('distShareBox').hidden = true;
      });
      function friendlyAuditStatusBadge(rec) {
        const st = String(rec.status || 'ok').toLowerCase();
        const code = Number(rec.status_code || 200);
        const span = document.createElement('span');
        if (st === 'ok' && code < 400) { span.className = 'chip ok'; span.textContent = '✓ 成功'; return span; }
        if (code === 429 || st === 'rate_limited') { span.className = 'chip warn'; span.textContent = '429 限流/配额'; return span; }
        if (code === 401 || code === 403 || st === 'forbidden') { span.className = 'chip err'; span.textContent = code + ' 权限拦截'; return span; }
        span.className = 'chip err';
        span.textContent = (code >= 400 ? (code + ' ') : '') + '执行异常';
        return span;
      }

      function renderAuditCenter(dist, usage, sessionsList) {
        const liveTasks = (dist && Array.isArray(dist.live_tasks)) ? dist.live_tasks : [];
        const sessions = Array.isArray(sessionsList) ? sessionsList : [];
        const recents = (usage && Array.isArray(usage.recent_requests)) ? usage.recent_requests : [];

        const navLive = document.getElementById('navLiveTasksChip');
        if (navLive) {
          navLive.className = liveTasks.length > 0 ? 'side-badge busy' : 'side-badge';
          navLive.textContent = liveTasks.length > 0 ? ('⚡ ' + liveTasks.length + ' 运行') : (sessions.length + ' 会话');
        }
        const ovAVal = document.getElementById('ovAuditVal');
        const ovASub = document.getElementById('ovAuditSub');
        const ovABadge = document.getElementById('ovAuditBadge');
        if (ovAVal) ovAVal.textContent = liveTasks.length > 0 ? (liveTasks.length + ' 项任务进行中') : (sessions.length + ' 个会话');
        if (ovASub) ovASub.textContent = '共 ' + sessions.length + ' 会话 · 最近 ' + recents.length + ' 条调用记录';
        if (ovABadge) {
          ovABadge.className = liveTasks.length > 0 ? 'chip busy' : 'chip';
          ovABadge.textContent = liveTasks.length > 0 ? '运行中 →' : '管理 →';
        }

        const sumChip = document.getElementById('auditSummaryChip');
        if (sumChip) {
          sumChip.className = liveTasks.length > 0 ? 'chip busy' : 'chip';
          sumChip.textContent = liveTasks.length + ' 在途 · ' + sessions.length + ' 会话 · ' + recents.length + ' 条记录';
        }

        // 1) Live In-Flight Strip (auto-expand when active)
        const liveFold = document.getElementById('liveTasksFold');
        const liveChip = document.getElementById('liveTasksCountChip');
        const liveBody = document.getElementById('liveTasksBody');
        if (liveChip) {
          liveChip.className = liveTasks.length > 0 ? 'chip busy' : 'chip ok';
          liveChip.textContent = liveTasks.length > 0 ? (liveTasks.length + ' 项正在运行/排队') : '当前空闲（0 项）';
        }
        if (liveFold && liveTasks.length > 0) liveFold.open = true;

        if (liveTasks.length === 0) {
          renderEmptyRow(liveBody, 6, '当前没有正在执行或排队的请求');
        } else {
          liveBody.replaceChildren();
          liveTasks.forEach(function (t) {
            const tr = document.createElement('tr');
            const pChip = document.createElement('span');
            const isRunning = (t.phase === 'running' || t.state === 'running');
            pChip.className = isRunning ? 'chip busy' : 'chip warn';
            pChip.textContent = (isRunning ? '生成中' : '排队等待') + (t.stream ? ' · 流式' : '');
            tr.appendChild(makeCell(pChip));

            const whoBox = document.createElement('div');
            const whoTop = document.createElement('strong');
            whoTop.textContent = friendlyTenantLabel(t.tenant_id, t.tenant_name);
            whoBox.appendChild(whoTop);
            if (t.client_ip) {
              const ipSub = document.createElement('div');
              ipSub.className = 'muted mono';
              ipSub.style.fontSize = '0.73rem';
              ipSub.textContent = t.client_ip;
              whoBox.appendChild(ipSub);
            }
            tr.appendChild(makeCell(whoBox));
            const tMeta = TASK_META[t.task_type] || { label: t.task_type || '调用' };
            tr.appendChild(makeCell(tMeta.label + ' · ' + friendlyModel(t.model)));
            tr.appendChild(makeCell(t.slot_key || t.tab_id ? friendlySeatLabel(t.slot_key, t.tab_id) : '等待分配'));

            const pq = document.createElement('div');
            pq.className = 'one-line-summary';
            pq.textContent = t.prompt_preview || '（无文本摘要）';
            tr.appendChild(makeCell(pq));
            tr.appendChild(makeCell(((Number(t.elapsed_ms || 0)) / 1000).toFixed(1) + ' 秒', 'num'));
            liveBody.appendChild(tr);
          });
        }

        // 2) Paginated & Expandable Sessions Table
        const sessTenSel = document.getElementById('sessionTenantFilter');
        syncTenantSelectOptions(sessTenSel, '全部调用方会话');
        const sessTenVal = (sessTenSel && sessTenSel.value) || 'all';
        const sessQuery = String((document.getElementById('sessionSearchInput') && document.getElementById('sessionSearchInput').value) || '').trim().toLowerCase();
        const purgeBtn = document.getElementById('sessionPurgeTenantBtn');
        if (purgeBtn) {
          purgeBtn.hidden = (sessTenVal === 'all');
          if (sessTenVal !== 'all') purgeBtn.textContent = '清空「' + friendlyTenantLabel(sessTenVal, '') + '」全部会话';
        }
        const filteredSessions = sessions.filter(function (s) {
          if (sessTenVal !== 'all' && (s.tenant_id || 'host') !== sessTenVal) return false;
          if (sessQuery) {
            const hay = [s.session_title, s.session_id, s.tenant_name, s.active_model].join(' ').toLowerCase();
            if (hay.indexOf(sessQuery) === -1) return false;
          }
          return true;
        });

        const sTabBadge = document.getElementById('sessionsTabBadge');
        if (sTabBadge) sTabBadge.textContent = String(filteredSessions.length);

        const sTotalPages = Math.max(1, Math.ceil(filteredSessions.length / sessionsPageSize));
        if (sessionsPage > sTotalPages) sessionsPage = sTotalPages;
        if (sessionsPage < 1) sessionsPage = 1;
        const sStart = (sessionsPage - 1) * sessionsPageSize;
        const pageSessions = filteredSessions.slice(sStart, sStart + sessionsPageSize);

        document.getElementById('sessionsPageInfo').textContent = filteredSessions.length > 0
          ? ('显示第 ' + (sStart + 1) + '-' + Math.min(filteredSessions.length, sStart + pageSessions.length) + ' 条 · 共 ' + filteredSessions.length + ' 条会话')
          : '共 0 条会话';
        document.getElementById('sessionsPageBadge').textContent = sessionsPage + ' / ' + sTotalPages;
        document.getElementById('sessionsPrevBtn').disabled = (sessionsPage <= 1);
        document.getElementById('sessionsNextBtn').disabled = (sessionsPage >= sTotalPages);

        const sessBody = document.getElementById('sessionsTableBody');
        if (pageSessions.length === 0) {
          renderEmptyRow(sessBody, 7, '当前筛选条件下暂无活跃或归档会话');
        } else {
          sessBody.replaceChildren();
          pageSessions.forEach(function (s, idx) {
            const sKey = String(s.session_ref || s.session_id || ('s-' + (sStart + idx)));
            const isExp = expandedSessionKeys.has(sKey);
            const tr = document.createElement('tr');
            if (isExp) tr.className = 'is-expanded';

            const stChip = document.createElement('span');
            if (s.busy || s.state === 'working') { stChip.className = 'chip busy'; stChip.textContent = s.activity_label || '工作中'; }
            else if (s.source === 'live') { stChip.className = 'chip ok'; stChip.textContent = '在线 (' + friendlySeatLabel(s.slot_key, s.tab_id) + ')'; }
            else { stChip.className = 'chip'; stChip.textContent = '历史归档'; }
            tr.appendChild(makeCell(stChip, 'nw'));
            tr.appendChild(makeCell(friendlyTenantLabel(s.tenant_id, s.tenant_name), 'nw'));

            const titleBtn = document.createElement('div');
            titleBtn.style.cursor = 'pointer';
            const tMain = document.createElement('div');
            tMain.className = 'one-line-summary';
            tMain.textContent = (isExp ? '▴ ' : '▾ ') + (s.session_title || '未命名会话');
            titleBtn.appendChild(tMain);
            if (s.session_id) {
              const sidSub = document.createElement('div');
              sidSub.className = 'one-line-sub mono';
              sidSub.textContent = 'ID: ' + s.session_id.slice(0, 18) + (s.session_id.length > 18 ? '…' : '');
              titleBtn.appendChild(sidSub);
            }
            titleBtn.addEventListener('click', function () {
              if (expandedSessionKeys.has(sKey)) expandedSessionKeys.delete(sKey);
              else expandedSessionKeys.add(sKey);
              renderAuditCenter(lastDistSnapshot, lastUsageSnapshot, lastSessionsOverview);
            });
            tr.appendChild(makeCell(titleBtn));

            const turnsTxt = friendlyModel(s.active_model) + ' · ' + (s.user_turns || Math.ceil(Number(s.turn_count || 0) / 2)) + ' 轮';
            tr.appendChild(makeCell(turnsTxt, 'nw'));
            const ctxTok = Number(s.total_context_tokens || 0);
            const cmpPct = Number(s.compact_pct || 0);
            tr.appendChild(makeCell(fmtTok(ctxTok) + ' (' + cmpPct.toFixed(0) + '%)', 'nw'));
            const updStr = s.updated_at ? new Date(s.updated_at * 1000).toLocaleString() : '-';
            tr.appendChild(makeCell(updStr, 'nw'));

            const delSessBtn = document.createElement('button');
            delSessBtn.type = 'button';
            delSessBtn.className = 'btn sm danger-soft';
            delSessBtn.textContent = s.source === 'live' ? '结束释放' : '删除';
            delSessBtn.disabled = Boolean(s.busy);
            delSessBtn.addEventListener('click', async function () {
              delSessBtn.disabled = true;
              try {
                const res = await fetch('/api/sessions/delete', {
                  method: 'POST', headers: buildAdminHeaders(), body: JSON.stringify({ session_ref: s.session_ref })
                });
                if (res.ok) await refreshStatus();
              } finally { delSessBtn.disabled = false; }
            });
            tr.appendChild(makeCell(delSessBtn, 'nw'));
            sessBody.appendChild(tr);

            if (isExp) {
              const dtr = document.createElement('tr');
              dtr.className = 'detail-expand-row';
              const dtd = document.createElement('td');
              dtd.colSpan = 7;
              const dCard = document.createElement('div');
              dCard.className = 'expand-detail-card';
              const fullTitle = document.createElement('div');
              fullTitle.innerHTML = '';
              const bLabel = document.createElement('strong');
              bLabel.textContent = '完整会话标题：';
              fullTitle.appendChild(bLabel);
              fullTitle.appendChild(document.createTextNode(s.session_title || '未命名会话'));
              dCard.appendChild(fullTitle);
              const metaLine = document.createElement('div');
              metaLine.className = 'expand-detail-meta';
              metaLine.textContent = '完整会话 ID: ' + (s.session_id || '自动会话')
                + '   |   已整理压缩: ' + Number(s.compaction_count || 0) + ' 次'
                + '   |   总交互步数: ' + Number(s.turn_count || 0) + ' 步';
              dCard.appendChild(metaLine);
              dtd.appendChild(dCard);
              dtr.appendChild(dtd);
              sessBody.appendChild(dtr);
            }
          });
        }

        // 3) Paginated & Expandable Audit Log Table
        const audTenSel = document.getElementById('auditTenantFilter');
        syncTenantSelectOptions(audTenSel, '全部调用方（本机 + 外部密钥）');
        const audTenVal = (audTenSel && audTenSel.value) || 'all';
        const audStVal = (document.getElementById('auditStatusFilter') && document.getElementById('auditStatusFilter').value) || 'all';
        const audQuery = String((document.getElementById('auditSearchInput') && document.getElementById('auditSearchInput').value) || '').trim().toLowerCase();

        const filteredAudit = recents.filter(function (r) {
          if (audTenVal !== 'all' && (r.tenant_id || 'host') !== audTenVal) return false;
          const code = Number(r.status_code || 200);
          const st = String(r.status || 'ok').toLowerCase();
          if (audStVal === 'ok' && !(st === 'ok' && code < 400)) return false;
          if (audStVal === 'blocked' && !(code === 401 || code === 403 || code === 429 || st === 'forbidden' || st === 'rate_limited')) return false;
          if (audStVal === 'error' && !(code >= 500 || (st === 'error' && code !== 401 && code !== 403 && code !== 429))) return false;
          if (audQuery) {
            const hay = [
              r.prompt_preview, r.reply_preview, r.error_message, r.error_code,
              r.session_title, r.session_id, r.tenant_name, r.client_ip, r.model, r.requested_model
            ].join(' ').toLowerCase();
            if (hay.indexOf(audQuery) === -1) return false;
          }
          return true;
        });

        const audChip = document.getElementById('auditCountChip');
        if (audChip) audChip.textContent = String(filteredAudit.length);

        const aTotalPages = Math.max(1, Math.ceil(filteredAudit.length / auditLogsPageSize));
        if (auditLogsPage > aTotalPages) auditLogsPage = aTotalPages;
        if (auditLogsPage < 1) auditLogsPage = 1;
        const aStart = (auditLogsPage - 1) * auditLogsPageSize;
        const pageAudit = filteredAudit.slice(aStart, aStart + auditLogsPageSize);

        document.getElementById('auditLogsPageInfo').textContent = filteredAudit.length > 0
          ? ('显示第 ' + (aStart + 1) + '-' + Math.min(filteredAudit.length, aStart + pageAudit.length) + ' 条 · 匹配 ' + filteredAudit.length + ' 条记录')
          : '共 0 条记录';
        document.getElementById('auditLogsPageBadge').textContent = auditLogsPage + ' / ' + aTotalPages;
        document.getElementById('auditLogsPrevBtn').disabled = (auditLogsPage <= 1);
        document.getElementById('auditLogsNextBtn').disabled = (auditLogsPage >= aTotalPages);

        const audBody = document.getElementById('auditLogBody');
        if (pageAudit.length === 0) {
          renderEmptyRow(audBody, 6, '当前筛选条件下暂无任务调用记录');
        } else {
          audBody.replaceChildren();
          pageAudit.forEach(function (r, idx) {
            const rKey = String((r.ts || 0) + ':' + (r.session_id || '') + ':' + (aStart + idx));
            const isExp = expandedAuditKeys.has(rKey);
            const tr = document.createElement('tr');
            if (isExp) tr.className = 'is-expanded';

            const tsBox = document.createElement('div');
            const tsStr = r.ts ? ((r.day ? r.day.slice(5) + ' ' : '') + new Date(r.ts * 1000).toLocaleTimeString()) : '-';
            const tsLine = document.createElement('div');
            tsLine.style.fontSize = '0.76rem';
            tsLine.style.marginBottom = '3px';
            tsLine.textContent = tsStr;
            tsBox.appendChild(tsLine);
            tsBox.appendChild(friendlyAuditStatusBadge(r));
            tr.appendChild(makeCell(tsBox, 'nw'));

            const callerBox = document.createElement('div');
            const callerStrong = document.createElement('strong');
            callerStrong.textContent = friendlyTenantLabel(r.tenant_id, r.tenant_name);
            callerBox.appendChild(callerStrong);
            if (r.client_ip) {
              const ipLine = document.createElement('div');
              ipLine.className = 'muted mono';
              ipLine.style.fontSize = '0.73rem';
              ipLine.textContent = r.client_ip;
              callerBox.appendChild(ipLine);
            }
            tr.appendChild(makeCell(callerBox, 'nw'));

            const mBox = document.createElement('div');
            const tMeta = TASK_META[r.task_type] || { label: r.task_label || '调用' };
            const mTop = document.createElement('div');
            mTop.style.fontWeight = '600';
            mTop.textContent = tMeta.label + ' · ' + friendlyModel(r.display_name || r.model);
            mBox.appendChild(mTop);
            const mSub = document.createElement('div');
            mSub.className = 'muted';
            mSub.style.fontSize = '0.74rem';
            mSub.textContent = (r.slot_key || r.tab_id) ? friendlySeatLabel(r.slot_key, r.tab_id) : '未进席位';
            mBox.appendChild(mSub);
            tr.appendChild(makeCell(mBox, 'nw'));

            const pv = document.createElement('div');
            pv.style.cursor = 'pointer';
            const qSummary = document.createElement('div');
            qSummary.className = 'one-line-summary';
            let shortPrompt = String(r.prompt_preview || '').replace(/\s+/g, ' ').trim();
            if (shortPrompt.indexOf('[System Instructions]') === 0) {
              shortPrompt = r.session_title ? ('[编程续聊] ' + r.session_title) : 'Copilot 编程与工具执行步骤';
            }
            qSummary.textContent = (isExp ? '▴ ' : '▾ ') + (shortPrompt ? ('问：' + shortPrompt) : '常规调用完成');
            pv.appendChild(qSummary);
            const ansText = r.error_message || r.reply_preview || '';
            if (ansText) {
              const aSummary = document.createElement('div');
              aSummary.className = 'one-line-sub';
              const cleanAns = String(ansText).replace(/```codex_tool_call[\s\S]*$/, '[调用本地工具]').replace(/\s+/g, ' ').trim();
              aSummary.textContent = (r.error_message ? '原因：' : '答：') + (cleanAns || ansText);
              pv.appendChild(aSummary);
            }
            pv.addEventListener('click', function () {
              if (expandedAuditKeys.has(rKey)) expandedAuditKeys.delete(rKey);
              else expandedAuditKeys.add(rKey);
              renderAuditCenter(lastDistSnapshot, lastUsageSnapshot, lastSessionsOverview);
            });
            tr.appendChild(makeCell(pv));

            const rt = resolveTokenParts(r);
            const tokBox = document.createElement('div');
            tokBox.style.fontWeight = '600';
            tokBox.textContent = rt.total > 0 ? (fmtLedgerTok(rt.total) + ' · ' + fmtMoney(r.cached_equivalent_cost_usd, r.cached_equivalent_cost_cny)) : '-';
            tr.appendChild(makeCell(tokBox, 'nw'));
            tr.appendChild(makeCell(((Number(r.duration_ms || 0)) / 1000).toFixed(1) + ' 秒', 'num'));
            audBody.appendChild(tr);

            if (isExp) {
              const dtr = document.createElement('tr');
              dtr.className = 'detail-expand-row';
              const dtd = document.createElement('td');
              dtd.colSpan = 6;
              const dCard = document.createElement('div');
              dCard.className = 'expand-detail-card';
              const qFull = document.createElement('div');
              const qB = document.createElement('strong');
              qB.textContent = '提问摘要：';
              qFull.appendChild(qB);
              qFull.appendChild(document.createTextNode(r.prompt_preview || '（无提问文本）'));
              dCard.appendChild(qFull);
              const aFull = document.createElement('div');
              const aB = document.createElement('strong');
              aB.textContent = r.error_message ? '拦截 / 异常原因：' : '回复摘要：';
              aFull.appendChild(aB);
              aFull.appendChild(document.createTextNode(ansText || '（正常返回）'));
              dCard.appendChild(aFull);
              const metaLine = document.createElement('div');
              metaLine.className = 'expand-detail-meta';
              metaLine.textContent = '会话: ' + (r.session_title || r.session_id || '-')
                + '   |   输入 ' + fmtLedgerTok(rt.input) + ' / 输出 ' + fmtLedgerTok(rt.output) + ' / 缓存 ' + fmtLedgerTok(rt.cached);
              dCard.appendChild(metaLine);
              dtd.appendChild(dCard);
              dtr.appendChild(dtd);
              audBody.appendChild(dtr);
            }
          });
        }
      }
      ['sessionTenantFilter', 'auditTenantFilter', 'auditStatusFilter'].forEach(function (id) {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('change', function () {
          if (id === 'sessionTenantFilter') sessionsPage = 1;
          else auditLogsPage = 1;
          renderAuditCenter(lastDistSnapshot, lastUsageSnapshot, lastSessionsOverview);
        });
      });
      ['sessionSearchInput', 'auditSearchInput'].forEach(function (id) {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('input', function () {
          if (id === 'sessionSearchInput') sessionsPage = 1;
          else auditLogsPage = 1;
          renderAuditCenter(lastDistSnapshot, lastUsageSnapshot, lastSessionsOverview);
        });
      });
      document.getElementById('sessionPageSizeSelect').addEventListener('change', function (ev) {
        sessionsPageSize = Math.max(5, Number(ev.target.value || 15));
        sessionsPage = 1;
        renderAuditCenter(lastDistSnapshot, lastUsageSnapshot, lastSessionsOverview);
      });
      document.getElementById('auditPageSizeSelect').addEventListener('change', function (ev) {
        auditLogsPageSize = Math.max(5, Number(ev.target.value || 15));
        auditLogsPage = 1;
        renderAuditCenter(lastDistSnapshot, lastUsageSnapshot, lastSessionsOverview);
      });
      document.getElementById('sessionsPrevBtn').addEventListener('click', function () {
        if (sessionsPage > 1) { sessionsPage -= 1; renderAuditCenter(lastDistSnapshot, lastUsageSnapshot, lastSessionsOverview); }
      });
      document.getElementById('sessionsNextBtn').addEventListener('click', function () {
        sessionsPage += 1;
        renderAuditCenter(lastDistSnapshot, lastUsageSnapshot, lastSessionsOverview);
      });
      document.getElementById('auditLogsPrevBtn').addEventListener('click', function () {
        if (auditLogsPage > 1) { auditLogsPage -= 1; renderAuditCenter(lastDistSnapshot, lastUsageSnapshot, lastSessionsOverview); }
      });
      document.getElementById('auditLogsNextBtn').addEventListener('click', function () {
        auditLogsPage += 1;
        renderAuditCenter(lastDistSnapshot, lastUsageSnapshot, lastSessionsOverview);
      });

      document.getElementById('sessionPurgeTenantBtn').addEventListener('click', async function () {
        const btn = document.getElementById('sessionPurgeTenantBtn');
        const tid = document.getElementById('sessionTenantFilter').value;
        if (!tid || tid === 'all') return;
        btn.disabled = true;
        try {
          const res = await fetch('/api/sessions/delete', {
            method: 'POST',
            headers: buildAdminHeaders(),
            body: JSON.stringify({ purge_tenant_id: tid, abort_inflight: false })
          });
          if (res.ok) await refreshStatus();
        } finally {
          btn.disabled = false;
        }
      });

      // Close open row dropdown menu when clicking outside
      document.addEventListener('click', function (ev) {
        if (!openClientMenuId) return;
        if (ev.target && ev.target.closest('.row-more-menu')) return;
        openClientMenuId = '';
        document.querySelectorAll('.row-more-menu[open]').forEach(function (el) { el.open = false; });
      });

      // Guide Modal & One-Click Copy
      const modal = document.getElementById('guideModal');
      function openGuideModal() { modal.hidden = false; }
      document.getElementById('openGuideBtn').addEventListener('click', openGuideModal);
      const topGuideBtn = document.getElementById('topGuideBtn');
      if (topGuideBtn) topGuideBtn.addEventListener('click', openGuideModal);
      document.getElementById('closeGuideBtn').addEventListener('click', function () { modal.hidden = true; });
      modal.addEventListener('click', function (ev) { if (ev.target === modal) modal.hidden = true; });
      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && !modal.hidden) modal.hidden = true;
        if (ev.key === 'Escape' && !clientEditModal.hidden) clientEditModal.hidden = true;
        if (ev.key === 'Escape' && !tunnelConfirmModal.hidden) closeTunnelConfirmModal();
      });

      document.querySelectorAll('[data-copy-target]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          const tid = btn.getAttribute('data-copy-target');
          const el = document.getElementById(tid);
          if (!el) return;
          copyTextWithFeedback(btn, el.textContent.trim(), '已复制 ✓');
        });
      });

      let seatCapSaving = false;
      let seatCapCustomEditing = false;
      let currentSeatCapValue = 10;
      let seatCapToastTimer = null;

      function syncDashSeatCapUI(capVal) {
        const group = document.getElementById('dashSeatCapGroup');
        if (!group) return;
        const num = Number(capVal);
        const safeNum = (Number.isFinite(num) && num >= 1) ? Math.min(200, Math.round(num)) : 10;
        currentSeatCapValue = safeNum;
        const normalized = String(safeNum);
        const isPreset = (safeNum === 1 || safeNum === 5 || safeNum === 10);
        const btns = group.querySelectorAll('.seat-cap-btn');
        btns.forEach(function (b) {
          const d = b.getAttribute('data-cap');
          if (d === 'custom') {
            b.classList.toggle('active', seatCapCustomEditing || !isPreset);
            b.textContent = !isPreset ? ('自定义 · ' + safeNum) : '自定义';
          } else {
            b.classList.toggle('active', !seatCapCustomEditing && d === normalized);
          }
        });
      }

      (function bindSeatCapControls() {
        const group = document.getElementById('dashSeatCapGroup');
        const labelEl = document.getElementById('dashSeatCapLabel');
        const customWrap = document.getElementById('dashSeatCapCustomWrap');
        const customInput = document.getElementById('dashSeatCapCustomInput');
        const customApply = document.getElementById('dashSeatCapCustomApply');
        if (!group) return;

        function showSeatCapToast(text, colorVar, ms) {
          if (!labelEl) return;
          labelEl.textContent = text;
          labelEl.style.color = colorVar || '';
          if (seatCapToastTimer) clearTimeout(seatCapToastTimer);
          seatCapToastTimer = setTimeout(function () {
            labelEl.textContent = '席位上限';
            labelEl.style.color = '';
          }, ms || 1800);
        }

        function closeCustomEditor() {
          seatCapCustomEditing = false;
          if (customWrap) customWrap.hidden = true;
          syncDashSeatCapUI(currentSeatCapValue);
        }

        function submitSeatCap(nextCap) {
          const allBtns = group.querySelectorAll('.seat-cap-btn');
          seatCapCustomEditing = false;
          if (customWrap) customWrap.hidden = true;
          seatCapSaving = true;
          allBtns.forEach(function (b) { b.disabled = true; });
          if (customInput) customInput.disabled = true;
          if (customApply) customApply.disabled = true;
          syncDashSeatCapUI(nextCap);
          const authVal = (document.getElementById('apiKeyInput').value || '').trim();
          const headers = { 'Content-Type': 'application/json' };
          if (authVal) headers['Authorization'] = 'Bearer ' + authVal;
          fetch('/api/seats/limit', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({ max_parallel_tabs: nextCap })
          }).then(async function (res) {
            const data = await res.json().catch(function () { return {}; });
            if (!res.ok || !data.ok) {
              throw new Error((data && data.detail) || ('HTTP ' + res.status));
            }
            syncDashSeatCapUI(data.max_parallel_tabs);
            showSeatCapToast('已设 ' + data.max_parallel_tabs + ' 席 ✓', 'var(--ok)', 1600);
          }).catch(function () {
            showSeatCapToast('切换失败', 'var(--err)', 2000);
          }).finally(function () {
            seatCapSaving = false;
            allBtns.forEach(function (b) { b.disabled = false; });
            if (customInput) customInput.disabled = false;
            if (customApply) customApply.disabled = false;
            refreshStatus();
          });
        }

        function confirmCustomSeatCap() {
          if (!customInput) return;
          const raw = (customInput.value || '').trim();
          if (!/^\d+$/.test(raw)) {
            showSeatCapToast('请输入 1~200 整数', 'var(--err)', 2200);
            customInput.focus();
            return;
          }
          const parsed = Number(raw);
          if (!Number.isInteger(parsed) || parsed < 1 || parsed > 200) {
            showSeatCapToast('范围应为 1~200', 'var(--err)', 2200);
            customInput.focus();
            return;
          }
          submitSeatCap(parsed);
        }

        group.addEventListener('click', function (ev) {
          const btn = ev.target && ev.target.closest('.seat-cap-btn');
          if (!btn || btn.disabled) return;
          const rawCap = btn.getAttribute('data-cap');
          if (!rawCap) return;
          if (rawCap === 'custom') {
            if (seatCapCustomEditing && customWrap && !customWrap.hidden) {
              closeCustomEditor();
              return;
            }
            seatCapCustomEditing = true;
            if (customWrap) customWrap.hidden = false;
            const isPreset = (currentSeatCapValue === 1 || currentSeatCapValue === 5 || currentSeatCapValue === 10);
            if (customInput) {
              customInput.value = isPreset ? '' : String(currentSeatCapValue);
              customInput.focus();
              if (!isPreset) customInput.select();
            }
            syncDashSeatCapUI(currentSeatCapValue);
            return;
          }
          submitSeatCap(Number(rawCap));
        });

        if (customApply) {
          customApply.addEventListener('click', function () { confirmCustomSeatCap(); });
        }
        if (customInput) {
          customInput.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter') { ev.preventDefault(); confirmCustomSeatCap(); }
            else if (ev.key === 'Escape') { ev.preventDefault(); closeCustomEditor(); }
          });
        }
      })();

      refreshStatus();
      setInterval(function () {
        if (!document.hidden) refreshStatus();
      }, 1500);
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) refreshStatus();
      });
    })();
  </script>
</body>
</html>
"""

_CACHED_HTML = DASHBOARD_HTML
_CACHED_MTIME_NS = 0


def get_dashboard_html() -> str:
    global _CACHED_HTML, _CACHED_MTIME_NS
    try:
        p = Path(__file__)
        st = p.stat()
        if st.st_mtime_ns != _CACHED_MTIME_NS:
            ns: dict = {}
            exec(compile(p.read_text(encoding="utf-8"), str(p), "exec"), ns)
            if isinstance(ns.get("DASHBOARD_HTML"), str):
                _CACHED_HTML = ns["DASHBOARD_HTML"]
                _CACHED_MTIME_NS = st.st_mtime_ns
    except Exception:
        pass
    return _CACHED_HTML
