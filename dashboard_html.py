"""Built-in local Web Console, Seat Monitor, Quick Health Check & Usage Ledger served at http://127.0.0.1:8000/."""

from __future__ import annotations

from pathlib import Path

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Gemini Enterprise · 本地控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7fc;
      --surface: #ffffff;
      --surface-soft: #f8fafd;
      --surface-muted: #f1f5fb;
      --border: #e3e9f4;
      --border-strong: #cbd6ea;
      --text: #0f172a;
      --text-sub: #334155;
      --muted: #64748b;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --primary-soft: #eff6ff;
      --indigo: #4f46e5;
      --ok: #059669;
      --ok-soft: #ecfdf5;
      --ok-border: #a7f3d0;
      --warn: #d97706;
      --warn-soft: #fffbeb;
      --warn-border: #fde68a;
      --err: #e11d48;
      --err-soft: #fff1f2;
      --err-border: #fecdd3;
      --shadow-card: 0 12px 32px -14px rgba(15, 23, 42, 0.07), 0 2px 6px rgba(15, 23, 42, 0.025);
      --shadow-hover: 0 16px 36px -14px rgba(37, 99, 235, 0.12), 0 4px 10px rgba(15, 23, 42, 0.04);
      --radius-xl: 20px;
      --radius-lg: 14px;
      --radius-md: 10px;
    }

    * { box-sizing: border-box; }
    [hidden] { display: none !important; }

    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "SF Pro Display", "SF Pro Text", "Segoe UI", system-ui, sans-serif;
      background:
        radial-gradient(900px 420px at 8% -6%, rgba(37, 99, 235, 0.075), transparent 60%),
        radial-gradient(820px 380px at 92% -4%, rgba(16, 185, 129, 0.065), transparent 58%),
        var(--bg);
      color: var(--text);
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
      font-variant-numeric: tabular-nums;
    }

    .wrap {
      max-width: 1120px;
      margin: 0 auto;
      padding: 28px 22px 56px;
    }

    /* Top Header */
    .topbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .brand-logo {
      width: 42px;
      height: 42px;
      border-radius: 13px;
      background: linear-gradient(135deg, #2563eb 0%, #4f46e5 100%);
      color: #ffffff;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 10px 22px -6px rgba(37, 99, 235, 0.45);
      flex-shrink: 0;
    }

    .brand h1 {
      margin: 0;
      font-size: 1.35rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      color: var(--text);
    }

    .brand-sub {
      margin-top: 2px;
      font-size: 0.84rem;
      color: var(--muted);
    }

    .topbar-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    .live-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      height: 36px;
      padding: 0 14px;
      border-radius: 999px;
      font-size: 0.82rem;
      font-weight: 600;
      background: var(--surface);
      border: 1px solid var(--border);
      box-shadow: 0 2px 8px rgba(15, 23, 42, 0.03);
      white-space: nowrap;
      transition: border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease;
    }
    .live-pill.flash {
      border-color: rgba(37, 99, 235, 0.36);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
    }

    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--warn);
      box-shadow: 0 0 0 4px rgba(217, 119, 6, 0.15);
    }
    .dot.ok { background: var(--ok); box-shadow: 0 0 0 4px rgba(5, 150, 105, 0.16); }
    .dot.err { background: var(--err); box-shadow: 0 0 0 4px rgba(225, 29, 72, 0.16); }

    /* Buttons */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      height: 36px;
      padding: 0 14px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text-sub);
      font: inherit;
      font-size: 0.84rem;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
      user-select: none;
      transition: transform 0.12s ease, background 0.16s ease, border-color 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
      text-decoration: none;
    }
    .btn:hover:not(:disabled) {
      border-color: var(--border-strong);
      background: var(--surface-soft);
      color: var(--text);
    }
    .btn:active:not(:disabled) {
      transform: scale(0.96);
    }
    .btn:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    .btn.primary {
      background: linear-gradient(135deg, #2563eb, #1d4ed8);
      border-color: transparent;
      color: #ffffff;
      box-shadow: 0 8px 20px -6px rgba(37, 99, 235, 0.42);
    }
    .btn.primary:hover:not(:disabled) {
      background: linear-gradient(135deg, #1d4ed8, #1e40af);
      transform: translateY(-1px);
    }
    .btn.soft {
      background: var(--primary-soft);
      border-color: rgba(37, 99, 235, 0.2);
      color: var(--primary);
    }
    .btn.soft:hover:not(:disabled) {
      background: #dbeafe;
    }
    .btn.sm {
      height: 28px;
      padding: 0 10px;
      font-size: 0.78rem;
      border-radius: 8px;
    }

    @keyframes refreshSpin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    .btn-refresh {
      min-width: 88px;
    }
    .refresh-icon {
      width: 15px;
      height: 15px;
      flex-shrink: 0;
      transition: transform 0.25s ease;
    }
    .btn-refresh:hover:not(:disabled):not(.is-refreshing) .refresh-icon {
      transform: rotate(35deg);
    }
    .btn-refresh.is-refreshing {
      border-color: rgba(37, 99, 235, 0.38);
      background: var(--primary-soft);
      color: var(--primary);
    }
    .btn-refresh.is-refreshing .refresh-icon {
      animation: refreshSpin 0.56s linear infinite;
    }
    .btn-refresh.is-refreshed {
      border-color: var(--ok-border);
      background: var(--ok-soft);
      color: var(--ok);
    }
    .btn-refresh.is-error {
      border-color: var(--err-border);
      background: var(--err-soft);
      color: var(--err);
    }

    /* 3-Step Self-Check Strip */
    .check-strip {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
      align-items: stretch;
    }
    @media (max-width: 820px) {
      .check-strip { grid-template-columns: 1fr; }
    }
    .step-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 15px;
      min-height: 60px;
      padding: 11px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      box-shadow: var(--shadow-card);
      transition: border-color 0.2s ease;
    }
    .step-card.ok { border-color: rgba(5, 150, 105, 0.26); }
    .step-card.warn { border-color: var(--warn-border); background: #fffdf7; }
    .step-card.err { border-color: var(--err-border); background: #fffafa; }
    .step-left {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      flex: 1;
    }
    .step-text {
      display: flex;
      align-items: baseline;
      gap: 8px;
      min-width: 0;
    }
    .step-right {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }
    .step-badge {
      width: 26px;
      height: 26px;
      border-radius: 8px;
      font-size: 0.78rem;
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
    .step-title {
      font-size: 0.86rem;
      font-weight: 700;
      color: var(--text);
      white-space: nowrap;
      flex-shrink: 0;
    }
    .step-desc {
      font-size: 0.78rem;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* Collapsible Sections */
    .fold-section {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-xl);
      box-shadow: var(--shadow-card);
      margin-bottom: 18px;
      overflow: hidden;
    }
    .fold-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 17px 22px;
      cursor: pointer;
      user-select: none;
      transition: background 0.15s ease;
    }
    .fold-head:hover {
      background: rgba(241, 245, 251, 0.55);
    }
    .fold-title-wrap {
      display: flex;
      align-items: center;
      gap: 11px;
      min-width: 0;
    }
    .sec-icon {
      width: 32px;
      height: 32px;
      border-radius: 10px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.95rem;
      flex-shrink: 0;
      background: var(--primary-soft);
      color: var(--primary);
    }
    .sec-icon.green { background: var(--ok-soft); color: var(--ok); }
    .sec-icon.indigo { background: #eef2ff; color: var(--indigo); }
    .sec-icon.slate { background: var(--surface-muted); color: var(--muted); }
    .fold-title {
      margin: 0;
      font-size: 1.03rem;
      font-weight: 750;
      color: var(--text);
    }
    .fold-subtitle {
      font-size: 0.8rem;
      color: var(--muted);
      margin-top: 1px;
    }
    .fold-right {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-shrink: 0;
    }
    .chevron {
      width: 26px;
      height: 26px;
      border-radius: 8px;
      background: var(--surface-muted);
      color: var(--muted);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.82rem;
      transition: transform 0.2s ease, background 0.15s ease;
    }
    .fold-section.collapsed .chevron {
      transform: rotate(-90deg);
    }
    .fold-body {
      padding: 0 22px 22px;
      border-top: 1px solid #eef2f9;
    }
    .fold-section.collapsed .fold-body {
      display: none;
    }

    /* Tags & Chips */
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
    .chip.purple { background: #eef2ff; color: var(--indigo); }

    /* Seat Detail Panel & 4-Column Seat Grid */
    .seat-detail-panel {
      margin-top: 16px;
      border-radius: var(--radius-lg);
      background: linear-gradient(180deg, #f8faff 0%, #f3f7fe 100%);
      border: 1px solid #e1eaf8;
      transition: all 0.18s ease;
    }
    .seat-detail-empty {
      padding: 12px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      font-size: 0.83rem;
      color: var(--muted);
    }
    .seat-detail-empty-left {
      display: flex;
      align-items: center;
      gap: 9px;
      min-width: 0;
    }
    .seat-detail-hint-dot {
      width: 24px;
      height: 24px;
      border-radius: 7px;
      background: #e8effc;
      color: var(--primary);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.78rem;
      font-weight: 700;
      flex-shrink: 0;
    }
    .seat-detail-content {
      padding: 15px 18px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .seat-detail-content[hidden],
    .seat-detail-empty[hidden] {
      display: none;
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
      max-width: 460px;
    }
    .seat-detail-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-shrink: 0;
    }
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
      background: #e5ecf6;
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
    @media (max-width: 920px) {
      .seat-detail-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      .seat-detail-kpis { grid-template-columns: 1fr; }
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
    .detail-kpi .k {
      font-size: 0.73rem;
      font-weight: 600;
      color: var(--muted);
      white-space: nowrap;
    }
    .detail-kpi .v {
      font-size: 0.83rem;
      font-weight: 700;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .detail-kpi .s {
      font-size: 0.73rem;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .seats-grid {
      margin-top: 14px;
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 11px;
      align-items: stretch;
    }
    @media (max-width: 1020px) {
      .seats-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 720px) {
      .seats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 480px) {
      .seats-grid { grid-template-columns: 1fr; }
    }
    .seat-card {
      background: var(--surface-soft);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
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
      border-color: #c7d6ee;
      transform: translateY(-1px);
    }
    .seat-card:focus-visible {
      outline: 2px solid var(--primary);
      outline-offset: 2px;
    }
    .seat-card.busy {
      background: linear-gradient(165deg, #eff6ff 0%, #f8fbff 100%);
      border-color: rgba(37, 99, 235, 0.44);
      box-shadow: 0 10px 24px -14px rgba(37, 99, 235, 0.28);
    }
    .seat-card.active-idle {
      background: #ffffff;
      border-color: #d5e0f2;
    }
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
    .seat-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 6px;
      height: 24px;
      min-width: 0;
    }
    .seat-name {
      font-weight: 700;
      font-size: 0.85rem;
      color: var(--text);
      display: inline-flex;
      align-items: center;
      gap: 5px;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .seat-topic {
      height: 46px;
      padding: 7px 9px;
      border-radius: 9px;
      background: rgba(241, 245, 251, 0.72);
      border: 1px solid #e9eef8;
      font-size: 0.8rem;
      font-weight: 650;
      line-height: 16px;
      color: var(--text);
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      word-break: break-word;
    }
    .seat-card.busy .seat-topic {
      background: rgba(255, 255, 255, 0.88);
      border-color: rgba(37, 99, 235, 0.22);
      color: #1e3a8a;
    }
    .seat-topic.empty {
      color: var(--muted);
      font-weight: 500;
      background: transparent;
      border-style: dashed;
    }
    .seat-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      height: 18px;
      font-size: 0.78rem;
      color: var(--muted);
      white-space: nowrap;
    }
    .seat-meta span {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .seat-mini-track {
      width: 100%;
      height: 5px;
      border-radius: 999px;
      background: #e6edf7;
      overflow: hidden;
    }
    .seat-mini-fill {
      height: 100%;
      width: 0%;
      border-radius: 999px;
      background: linear-gradient(90deg, #10b981, #3b82f6);
    }
    .idle-fold-wrap {
      margin-top: 12px;
      border-radius: 13px;
      border: 1px solid var(--border);
      background: #fafcff;
      overflow: hidden;
    }
    .idle-fold-Wrap-hidden,
    .idle-fold-wrap[hidden] {
      display: none;
    }
    .idle-fold-btn {
      width: 100%;
      border: none;
      background: transparent;
      padding: 10px 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      font: inherit;
      font-size: 0.82rem;
      font-weight: 650;
      color: var(--text-sub);
      cursor: pointer;
      transition: background 0.15s ease;
    }
    .idle-fold-btn:hover {
      background: #f1f6fd;
    }
    .idle-fold-hint {
      font-size: 0.77rem;
      font-weight: 600;
      color: var(--muted);
    }
    .idle-fold-body {
      padding: 0 14px 14px;
      border-top: 1px dashed var(--border);
    }
    .idle-fold-body[hidden] {
      display: none;
    }
    .empty-seats {
      grid-column: 1 / -1;
      text-align: center;
      padding: 16px;
      border-radius: 13px;
      background: var(--surface-soft);
      border: 1px dashed var(--border-strong);
      color: var(--muted);
      font-size: 0.84rem;
    }

    /* One-Click Health Test */
    .probe-bar {
      margin-top: 16px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-radius: var(--radius-lg);
      background: var(--surface-soft);
      border: 1px solid var(--border);
    }
    .probe-controls {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }
    select.clean-select,
    input.clean-input,
    textarea.clean-textarea {
      padding: 8px 12px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--text);
      font: inherit;
      font-size: 0.85rem;
      outline: none;
      transition: border-color 0.15s ease;
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
    }
    .probe-result {
      margin-top: 12px;
      padding: 13px 16px;
      border-radius: 13px;
      border: 1px solid var(--border);
      background: #ffffff;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .probe-result.ok {
      background: var(--ok-soft);
      border-color: var(--ok-border);
    }
    .probe-result.err {
      background: var(--err-soft);
      border-color: var(--err-border);
    }
    .probe-res-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 0.88rem;
      font-weight: 700;
    }
    .probe-res-body {
      margin: 0;
      font-size: 0.86rem;
      color: var(--text-sub);
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 260px;
      overflow: auto;
    }
    .custom-test-drawer {
      margin-top: 12px;
      border: 1px dashed var(--border-strong);
      border-radius: 13px;
      padding: 10px 14px;
      background: #fafcff;
    }
    .custom-test-drawer summary {
      cursor: pointer;
      font-size: 0.83rem;
      font-weight: 600;
      color: var(--muted);
      user-select: none;
    }
    .custom-test-body {
      margin-top: 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    /* Ledger Controls & Hero */
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
      font-size: 0.76rem;
      font-weight: 700;
      padding: 3px 10px;
      border-radius: 999px;
      cursor: pointer;
      transition: all 0.14s ease;
    }
    .cur-btn.active {
      background: #ffffff;
      color: var(--primary);
      box-shadow: 0 1px 4px rgba(15, 23, 42, 0.08);
    }

    .period-bar {
      margin-top: 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      padding: 6px 10px;
      border-radius: 12px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
    }
    .period-tabs {
      display: flex;
      gap: 5px;
      flex-wrap: wrap;
    }
    .period-tab {
      background: transparent;
      color: var(--muted);
      border: none;
      border-radius: 8px;
      height: 28px;
      padding: 0 10px;
      display: inline-flex;
      align-items: center;
      font: inherit;
      font-size: 0.79rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.14s ease;
    }
    .period-tab:hover {
      color: var(--text);
      background: #eaf0fa;
    }
    .period-tab.active {
      background: var(--primary);
      color: #ffffff;
      box-shadow: 0 4px 10px -3px rgba(37, 99, 235, 0.35);
    }
    .date-picker-wrap {
      display: flex;
      align-items: center;
      gap: 7px;
      font-size: 0.78rem;
      color: var(--muted);
    }
    input[type="date"].date-input {
      height: 28px;
      padding: 0 8px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--text);
      font: inherit;
      font-size: 0.78rem;
    }

    .ledger-hero {
      margin-top: 12px;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 11px;
      align-items: stretch;
    }
    @media (max-width: 980px) {
      .ledger-hero { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      .ledger-hero { grid-template-columns: 1fr; }
    }
    .stat-tile {
      padding: 12px 13px;
      border-radius: 14px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 5px;
      min-width: 0;
      transition: background 0.15s ease, border-color 0.15s ease;
    }
    .stat-tile:hover {
      background: #ffffff;
      border-color: #d5e0f2;
    }
    .stat-icon-box {
      width: 22px;
      height: 22px;
      flex-shrink: 0;
      border-radius: 7px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .stat-icon-box.emerald {
      background: var(--ok-soft);
      color: var(--ok);
    }
    .stat-icon-box.blue {
      background: var(--primary-soft);
      color: var(--primary);
    }
    .stat-icon-box.teal {
      background: #f0fdfa;
      color: #0d9488;
    }
    .stat-icon-box.indigo {
      background: #eef2ff;
      color: var(--indigo);
    }
    .stat-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      height: 22px;
      font-size: 0.77rem;
      font-weight: 650;
      color: var(--muted);
      white-space: nowrap;
    }
    .stat-big {
      height: 24px;
      line-height: 24px;
      font-size: 1.06rem;
      font-weight: 750;
      letter-spacing: -0.015em;
      color: var(--text);
      margin: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-variant-numeric: tabular-nums;
    }
    .stat-sub {
      height: 17px;
      line-height: 17px;
      font-size: 0.73rem;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-variant-numeric: tabular-nums;
    }

    /* Visual Charts Grid */
    .charts-grid {
      margin-top: 12px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 11px;
      align-items: stretch;
    }
    @media (max-width: 860px) {
      .charts-grid { grid-template-columns: 1fr; }
    }
    .chart-box {
      padding: 12px 14px;
      border-radius: 14px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 10px;
      min-width: 0;
    }
    .chart-title-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      min-height: 20px;
      font-size: 0.83rem;
      font-weight: 700;
      color: var(--text);
    }
    .chart-legend {
      display: flex;
      gap: 10px;
      font-size: 0.74rem;
      font-weight: 500;
      color: var(--muted);
    }
    .leg-dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 3px;
      margin-right: 4px;
      vertical-align: middle;
    }
    .bars-stage {
      flex: 1;
      min-height: 132px;
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
      max-width: 34px;
      flex: 1;
      min-height: 96px;
      max-height: 156px;
      display: flex;
      flex-direction: column;
      justify-content: flex-end;
      border-radius: 7px 7px 3px 3px;
      overflow: hidden;
      background: #eaf0f9;
      transition: transform 0.15s ease;
    }
    .bar-val {
      font-size: 0.69rem;
      font-weight: 600;
      color: var(--text-sub);
      white-space: nowrap;
      min-height: 15px;
    }
    .bar-col:hover .bar-stack {
      transform: translateY(-2px);
    }
    .bar-col.selected .bar-lbl {
      color: var(--primary);
      font-weight: 700;
    }
    .bar-seg-actual {
      background: linear-gradient(180deg, #3b82f6 0%, #2563eb 100%);
      width: 100%;
      border-radius: 6px 6px 2px 2px;
      transition: height 0.25s ease;
    }
    .bar-lbl {
      font-size: 0.71rem;
      color: var(--muted);
      white-space: nowrap;
    }

    .task-ratio-bar {
      width: 100%;
      height: 12px;
      border-radius: 999px;
      background: #e6edf7;
      overflow: hidden;
      display: flex;
      gap: 2px;
    }
    .task-list {
      display: flex;
      flex-direction: column;
      gap: 7px;
    }
    .task-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 0.8rem;
    }
    .task-row-left {
      display: flex;
      align-items: center;
      gap: 7px;
      color: var(--text-sub);
      font-weight: 600;
    }
    .task-row-right {
      color: var(--muted);
      font-size: 0.77rem;
    }

    /* Tables */
    details.sub-panel {
      margin-top: 14px;
      border-top: 1px dashed var(--border);
      padding-top: 12px;
    }
    details.sub-panel summary {
      cursor: pointer;
      font-size: 0.83rem;
      font-weight: 650;
      color: var(--text-sub);
      user-select: none;
    }
    .table-scroll { overflow-x: auto; margin-top: 10px; }
    .sub-table-title {
      margin: 14px 0 6px;
      font-size: 0.78rem;
      font-weight: 700;
      color: var(--text-sub);
    }
    table.clean-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.8rem;
    }
    table.clean-table th,
    table.clean-table td {
      padding: 8px 10px;
      text-align: left;
      border-bottom: 1px solid #edf2f9;
      white-space: nowrap;
    }
    table.clean-table th.num,
    table.clean-table td.num {
      text-align: right;
    }
    table.clean-table th {
      color: var(--muted);
      font-weight: 600;
      background: var(--surface-soft);
    }
    table.clean-table tbody tr:hover {
      background: #f9fbff;
    }

    /* Diagnostics & Logs */
    .diag-kv-grid {
      margin-top: 14px;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      align-items: stretch;
    }
    @media (max-width: 920px) {
      .diag-kv-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      .diag-kv-grid { grid-template-columns: 1fr; }
    }
    .diag-kv {
      padding: 11px 14px;
      border-radius: 12px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
      font-size: 0.8rem;
      min-width: 0;
    }
    .diag-kv .k { color: var(--muted); font-size: 0.74rem; white-space: nowrap; }
    .diag-kv .v { font-weight: 650; color: var(--text); margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .logs-box {
      margin-top: 12px;
      max-height: 180px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.77rem;
      background: var(--surface-soft);
      border-radius: 11px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      color: var(--text-sub);
    }
    .log-item { padding: 3px 0; border-bottom: 1px solid #eef2f9; }
    .log-item:last-child { border-bottom: none; }

    /* Public Distribution & Multi-Client Routing */
    .sec-icon.teal { background: #f0fdfa; color: #0d9488; }
    .btn.danger-soft {
      background: var(--err-soft);
      border-color: var(--err-border);
      color: var(--err);
    }
    .btn.danger-soft:hover:not(:disabled) {
      background: #ffe4e6;
    }
    .dist-card {
      margin-top: 16px;
      padding: 15px 16px;
      border-radius: var(--radius-lg);
      background: var(--surface-soft);
      border: 1px solid var(--border);
    }
    .dist-card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .dist-card-title {
      font-size: 0.88rem;
      font-weight: 700;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .dist-form-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      align-items: end;
    }
    .dist-create-grid {
      display: grid;
      grid-template-columns: 1.5fr 0.8fr 0.9fr 0.9fr 1fr 1.2fr auto;
      gap: 10px;
      align-items: end;
    }
    @media (max-width: 980px) {
      .dist-form-grid,
      .dist-create-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    @media (max-width: 560px) {
      .dist-form-grid,
      .dist-create-grid {
        grid-template-columns: 1fr;
      }
    }
    .dist-field {
      display: flex;
      flex-direction: column;
      gap: 4px;
      min-width: 0;
    }
    .dist-field label {
      font-size: 0.74rem;
      font-weight: 600;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .dist-field input.clean-input,
    .dist-field select.clean-select {
      width: 100%;
    }
    .dist-share-box {
      margin-top: 14px;
      padding: 14px 16px;
      border-radius: 13px;
      background: linear-gradient(180deg, #f0fdfa 0%, #f8fafc 100%);
      border: 1px solid #99f6e4;
    }
    .dist-share-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 8px;
      font-size: 0.85rem;
      font-weight: 700;
      color: #0f766e;
    }
    .dist-share-pre {
      margin: 0;
      padding: 10px 12px;
      border-radius: 10px;
      background: #ffffff;
      border: 1px solid #ccfbf1;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.79rem;
      color: var(--text);
      white-space: pre-wrap;
      word-break: break-all;
      line-height: 1.5;
    }
    .dist-row-actions {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }

    /* Modal */
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.36);
      backdrop-filter: blur(4px);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
      z-index: 100;
    }
    .modal-backdrop[hidden] { display: none; }
    .modal-card {
      width: 100%;
      max-width: 540px;
      background: #ffffff;
      border-radius: 20px;
      border: 1px solid var(--border);
      box-shadow: 0 24px 60px -12px rgba(15, 23, 42, 0.22);
      padding: 22px 24px;
    }
    .modal-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
    }
    .modal-head h3 { margin: 0; font-size: 1.08rem; }
    .copy-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 10px 12px;
      border-radius: 12px;
      background: var(--surface-soft);
      border: 1px solid var(--border);
      margin-bottom: 9px;
    }
    .copy-meta { min-width: 0; }
    .copy-label { font-size: 0.75rem; color: var(--muted); }
    .copy-val {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--text);
      word-break: break-all;
    }

    .muted { color: var(--muted); }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  </style>
</head>
<body>
  <div class="wrap">
    <!-- Top Header -->
    <header class="topbar">
      <div class="brand">
        <div class="brand-logo" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
            <path d="M12 2C12.9 7.8 16.2 11.1 22 12C16.2 12.9 12.9 16.2 12 22C11.1 16.2 7.8 12.9 2 12C7.8 11.1 11.1 7.8 12 2Z" fill="currentColor"/>
          </svg>
        </div>
        <div>
          <h1>Gemini Enterprise</h1>
          <div class="brand-sub">本地网关控制台 · 默认 Carbon</div>
        </div>
      </div>
      <div class="topbar-actions">
        <div class="live-pill" id="connBadge">
          <span class="dot" id="connDot"></span>
          <span id="connText">正在同步状态...</span>
        </div>
        <button type="button" class="btn soft" id="openGuideBtn">接入指南</button>
        <button type="button" class="btn btn-refresh" id="refreshBtn" title="立即同步最新状态">
          <svg class="refresh-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M20 11A8 8 0 0 0 6.34 6.34L4 8.68M4 4.5V8.7H8.2M4 13A8 8 0 0 0 17.66 17.66L20 15.32M20 19.5V15.3H15.8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <span id="refreshBtnText">刷新</span>
        </button>
      </div>
    </header>

    <!-- 3-Step Self-Check Strip -->
    <section class="check-strip" aria-label="连接三步自检">
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
    </section>

    <!-- Section 1: Parallel Seats & Memory Capacity -->
    <section class="fold-section" data-sec="seats" id="secSeats">
      <div class="fold-head" data-fold-toggle="seats">
        <div class="fold-title-wrap">
          <span class="sec-icon">✦</span>
          <div>
            <h2 class="fold-title">并行工作席位</h2>
            <div class="fold-subtitle">仅展开展示活跃席位 · 点击任意席位卡片查看完整详情与会话操作</div>
          </div>
        </div>
        <div class="fold-right">
          <span class="chip" id="seatsSummaryChip">0 个席位待命</span>
          <span class="chevron" aria-hidden="true">▾</span>
        </div>
      </div>
      <div class="fold-body">
        <div class="seat-detail-panel" id="seatDetailPanel">
          <div class="seat-detail-empty" id="seatDetailEmpty">
            <div class="seat-detail-empty-left">
              <span class="seat-detail-hint-dot" aria-hidden="true">i</span>
              <span>点击下方任意席位小卡片，在此查看记忆池余量、本轮收发明细、网页标签状态与重置操作</span>
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
                <button type="button" class="btn sm" id="detailCloseBtn">收起</button>
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

    <!-- Section 2: One-Click Model Health Check -->
    <section class="fold-section" data-sec="probe" id="secProbe">
      <div class="fold-head" data-fold-toggle="probe">
        <div class="fold-title-wrap">
          <span class="sec-icon green">⚡</span>
          <div>
            <h2 class="fold-title">一键模型连通体检</h2>
            <div class="fold-subtitle">无需手动想问题打字，点一下自动验证模型真实回复与响应速度</div>
          </div>
        </div>
        <div class="fold-right">
          <span class="chip" id="probeHeaderChip">默认测 Carbon</span>
          <span class="chevron" aria-hidden="true">▾</span>
        </div>
      </div>
      <div class="fold-body">
        <div class="probe-bar">
          <div class="probe-controls">
            <select id="modelSelect" class="clean-select" aria-label="选择测试模型">
              <option value="gemini-4-carbon" selected>Carbon（默认主力）</option>
              <option value="gemini-4-argon">Argon（高速轻量）</option>
              <option value="gemini-3.1-pro">Gemini 3.1 Pro</option>
              <option value="gemini-3.8-flash">Gemini 3.8 Flash</option>
              <option value="gemini-enterprise-web">保持网页当前模型</option>
              <optgroup label="其他兼容模型">
                <option value="gemini-2.5-pro">Gemini 2.5 Pro</option>
                <option value="gemini-2.5-flash">Gemini 2.5 Flash</option>
              </optgroup>
            </select>
            <button type="button" class="btn primary" id="quickProbeBtn">⚡ 一键快速体检</button>
          </div>
          <span class="muted" style="font-size:0.8rem;" id="probeHint">自动发送简短口令并核验真实网页回复</span>
        </div>

        <div class="probe-result" id="probeResultBox" hidden>
          <div class="probe-res-head">
            <span id="probeResTitle">测试结果</span>
            <span class="chip" id="probeResMeta">-</span>
          </div>
          <pre class="probe-res-body" id="replyOut">等待测试输出...</pre>
        </div>

        <details class="custom-test-drawer" id="customTestDrawer">
          <summary>需要自己提问或测试图片 / 文档附件？点此展开自定义测试</summary>
          <div class="custom-test-body">
            <textarea id="promptInput" class="clean-textarea" rows="3" placeholder="输入你想测试的问题，也可在下方选择图片、PDF、Word、表格或音视频一起发送..."></textarea>
            <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
              <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                <input id="fileInput" type="file" multiple style="font-size:0.82rem;color:var(--muted);" />
                <details style="font-size:0.78rem;color:var(--muted);">
                  <summary style="cursor:pointer;">高级：填写访问密钥（本机默认留空）</summary>
                  <input id="apiKeyInput" class="clean-input" type="password" autocomplete="off" spellcheck="false" placeholder="默认留空免密" style="margin-top:6px;width:240px;" />
                </details>
              </div>
              <button type="button" class="btn soft" id="sendBtn">发送自定义提问</button>
            </div>
          </div>
        </details>
      </div>
    </section>

    <!-- Section 3: Usage & Reference Cost Ledger -->
    <section class="fold-section" data-sec="ledger" id="secLedger">
      <div class="fold-head" data-fold-toggle="ledger">
        <div class="fold-title-wrap">
          <span class="sec-icon indigo">¥</span>
          <div>
            <h2 class="fold-title">用量与费用账本</h2>
            <div class="fold-subtitle">基于本地字数估算与参考价格表折算（非 Google 实际扣费）</div>
          </div>
        </div>
        <div class="fold-right">
          <div class="currency-switch" id="currencySwitch" title="切换显示币种">
            <button type="button" class="cur-btn active" data-cur="CNY">¥ 人民币</button>
            <button type="button" class="cur-btn" data-cur="USD">$ 美元</button>
          </div>
          <span class="chevron" aria-hidden="true">▾</span>
        </div>
      </div>
      <div class="fold-body">
        <div class="period-bar">
          <div class="period-tabs" id="periodTabs">
            <button type="button" class="period-tab" data-period="today">今日</button>
            <button type="button" class="period-tab" data-period="yesterday">昨日</button>
            <button type="button" class="period-tab" data-period="7d">近 7 天</button>
            <button type="button" class="period-tab" data-period="30d">近 30 天</button>
            <button type="button" class="period-tab" data-period="session">本次启动</button>
            <button type="button" class="period-tab active" data-period="all">全部累计</button>
          </div>
          <div class="date-picker-wrap">
            <span>按日历查某天:</span>
            <input type="date" id="datePickerInput" class="date-input" aria-label="按日期查看统计" />
            <select id="dayFilterSelect" hidden></select>
            <span class="chip" id="activePeriodTag">全部累计</span>
          </div>
        </div>

        <div class="ledger-hero">
          <div class="stat-tile">
            <div class="stat-head">
              <span id="cardActualLabel">预估等效花费</span>
              <div class="stat-icon-box emerald" aria-hidden="true">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
                  <rect x="3" y="6" width="18" height="13" rx="3" stroke="currentColor" stroke-width="2"/>
                  <path d="M16 12.5H21" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                  <circle cx="16" cy="12.5" r="1.2" fill="currentColor"/>
                  <path d="M6 6L14.5 3.4C15.6 3.1 16.7 3.9 16.7 5V6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
                </svg>
              </div>
            </div>
            <div class="stat-big" id="ledgerActualMain">¥0.00</div>
            <div class="stat-sub" id="ledgerActualSub">输入 / 缓存 / 输出单价折算</div>
          </div>

          <div class="stat-tile">
            <div class="stat-head">
              <span id="cardCallsLabel">调用总次数</span>
              <div class="stat-icon-box blue" aria-hidden="true">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
                  <path d="M13 2L4.5 13.2H11.5L10.5 22L19.5 10.6H12.5L13 2Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
                </svg>
              </div>
            </div>
            <div class="stat-big" id="ledgerCallsMain">0 次</div>
            <div class="stat-sub" id="ledgerCallsSub">Codex 0 / 对话 0 / 整理 0</div>
          </div>

          <div class="stat-tile">
            <div class="stat-head">
              <span id="cardCacheLabel">缓存命中率</span>
              <div class="stat-icon-box teal" aria-hidden="true">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
                  <path d="M12 3L4 7.5L12 12L20 7.5L12 3Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
                  <path d="M4 12.5L12 17L20 12.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                  <path d="M4 17L12 21.5L20 17" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </div>
            </div>
            <div class="stat-big" id="ledgerCacheMain">0.0%</div>
            <div class="stat-sub" id="ledgerCacheSub">缓存 0 / 总输入 0</div>
          </div>

          <div class="stat-tile">
            <div class="stat-head">
              <span id="cardTokensLabel">累计 Token</span>
              <div class="stat-icon-box indigo" aria-hidden="true">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
                  <ellipse cx="12" cy="5.5" rx="7.5" ry="3" stroke="currentColor" stroke-width="2"/>
                  <path d="M4.5 8.8C4.5 10.46 7.86 11.8 12 11.8C16.14 11.8 19.5 10.46 19.5 8.8V12.2C19.5 13.86 16.14 15.2 12 15.2C7.86 15.2 4.5 13.86 4.5 12.2V8.8Z" fill="currentColor"/>
                  <path d="M4.5 14.6C4.5 16.26 7.86 17.6 12 17.6C16.14 17.6 19.5 16.26 19.5 14.6V18C19.5 19.66 16.14 21 12 21C7.86 21 4.5 19.66 4.5 18V14.6Z" fill="currentColor"/>
                </svg>
              </div>
            </div>
            <div class="stat-big" id="ledgerTokensMain">0</div>
            <div class="stat-sub" id="ledgerTokensSub">输入 0 / 输出 0 / 缓存 0</div>
          </div>
        </div>

        <div class="charts-grid">
          <div class="chart-box">
            <div class="chart-title-row">
              <span>每日花费趋势（点击柱子可看当天明细）</span>
              <div class="chart-legend">
                <span><i class="leg-dot" style="background:#2563eb;"></i>等效花费</span>
              </div>
            </div>
            <div class="bars-stage" id="dailyBarsStage"></div>
          </div>

          <div class="chart-box">
            <div class="chart-title-row">
              <span>用途分布占比</span>
              <span class="muted" style="font-size:0.74rem;" id="ledgerRateBadge">Carbon 参考价</span>
            </div>
            <div class="task-ratio-bar" id="taskRatioBar"></div>
            <div class="task-list" id="taskBreakdownList"></div>
          </div>
        </div>

        <details class="sub-panel">
          <summary>查看分模型统计、按日汇总与最近调用流水</summary>
          <div class="sub-table-title">按模型汇总</div>
          <div class="table-scroll">
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

          <div class="sub-table-title">按日期汇总</div>
          <div class="table-scroll">
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

          <div class="sub-table-title">最近调用流水</div>
          <div class="table-scroll">
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
        </details>
      </div>
    </section>

    <!-- Section 3.5: Public Distribution & Multi-Client Routing -->
    <section class="fold-section" data-sec="dist" id="secDist">
      <div class="fold-head" data-fold-toggle="dist">
        <div class="fold-title-wrap">
          <span class="sec-icon teal">⇄</span>
          <div>
            <h2 class="fold-title">公网分发与多客分流</h2>
            <div class="fold-subtitle">对方无需安装任何插件，填入地址与专属密钥即可使用 · 自动隔离会话并为本机预留专属席位</div>
          </div>
        </div>
        <div class="fold-right">
          <span class="chip" id="distSummaryChip">本机预留 2 席 · 0 把密钥</span>
          <span class="chevron" aria-hidden="true">▾</span>
        </div>
      </div>
      <div class="fold-body">
        <div class="dist-card">
          <div class="dist-card-head">
            <div class="dist-card-title">
              <span>主机保护与公网分流策略</span>
              <span class="chip ok" id="distAccessBadge">外部共享已开启</span>
              <span class="muted" style="font-weight:500;font-size:0.79rem;" id="distPoolSplitText">本机免密直连不受任何限制</span>
            </div>
            <div style="display:flex;align-items:center;gap:8px;">
              <span class="muted" style="font-size:0.77rem;" id="distPolicyMsg"></span>
              <button type="button" class="btn sm primary" id="distSavePolicyBtn">保存分流设置</button>
            </div>
          </div>
          <div class="dist-form-grid">
            <div class="dist-field">
              <label for="distEnabledSelect">外部接入总开关</label>
              <select id="distEnabledSelect" class="clean-select">
                <option value="1">允许接入（凭下方专属密钥）</option>
                <option value="0">暂停外部接入（仅本机可用）</option>
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
              <label for="distPublicUrlInput">公网访问域名（选填，用于生成接入包）</label>
              <input id="distPublicUrlInput" class="clean-input" type="text" spellcheck="false" placeholder="如 https://ai.example.com" />
            </div>
          </div>
        </div>

        <div class="dist-card" style="margin-top:12px;">
          <div class="dist-card-head">
            <div class="dist-card-title">
              <span>新建客户端专属密钥</span>
              <span class="muted" style="font-weight:500;font-size:0.78rem;">每人一把独立密钥，对话记忆、上传文件与并发排队互不串扰</span>
            </div>
          </div>
          <div class="dist-create-grid">
            <div class="dist-field">
              <label for="distClientName">使用者名称</label>
              <input id="distClientName" class="clean-input" type="text" maxlength="64" placeholder="如：朋友小张 / 副机笔记本" />
            </div>
            <div class="dist-field">
              <label for="distClientConcurrency">单客最大并发</label>
              <input id="distClientConcurrency" class="clean-input" type="number" min="1" max="20" value="2" />
            </div>
            <div class="dist-field">
              <label for="distClientRpm">每分钟限次</label>
              <input id="distClientRpm" class="clean-input" type="number" min="1" max="600" value="30" />
            </div>
            <div class="dist-field">
              <label for="distClientDailyReq">每日次数上限</label>
              <input id="distClientDailyReq" class="clean-input" type="number" min="0" value="0" title="填 0 表示不限制每日次数" />
            </div>
            <div class="dist-field">
              <label for="distClientDailyTok">每日 Token 上限</label>
              <input id="distClientDailyTok" class="clean-input" type="number" min="0" value="0" title="填 0 表示不限制每日 Token" />
            </div>
            <div class="dist-field">
              <label for="distClientNote">备注（选填）</label>
              <input id="distClientNote" class="clean-input" type="text" maxlength="120" placeholder="例如：仅日常编程使用" />
            </div>
            <button type="button" class="btn primary" id="distCreateClientBtn">+ 生成专属密钥</button>
          </div>
        </div>

        <div class="dist-share-box" id="distShareBox" hidden>
          <div class="dist-share-head">
            <span id="distShareTitle">专属接入配置包（可直接发给对方）</span>
            <div class="dist-row-actions">
              <button type="button" class="btn sm primary" id="distCopyBundleBtn">复制完整接入包</button>
              <button type="button" class="btn sm soft" id="distCopyKeyOnlyBtn">仅复制密钥</button>
              <button type="button" class="btn sm" id="distCloseShareBtn">收起</button>
            </div>
          </div>
          <pre class="dist-share-pre" id="distSharePre"></pre>
        </div>

        <div class="table-scroll" style="margin-top:14px;">
          <table class="clean-table" id="distClientsTable">
            <thead>
              <tr>
                <th>客户端名称</th>
                <th>专属密钥</th>
                <th>实时并发</th>
                <th>限流与今日配额</th>
                <th>累计调用 · 最近活跃</th>
                <th>管理操作</th>
              </tr>
            </thead>
            <tbody id="distClientsBody">
              <tr><td colspan="6" class="muted">暂未创建外部密钥 · 本机直连免密，如需分享给他人请在上方生成专属密钥</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- Section 4: Diagnostics & Logs (Collapsed by default) -->
    <section class="fold-section collapsed" data-sec="diag" id="secDiag">
      <div class="fold-head" data-fold-toggle="diag">
        <div class="fold-title-wrap">
          <span class="sec-icon slate">⚙</span>
          <div>
            <h2 class="fold-title">系统诊断与底层运行记录</h2>
            <div class="fold-subtitle">平时无需关注；遇到连接异常时可展开查看底层通道与网关日志</div>
          </div>
        </div>
        <div class="fold-right">
          <span class="chip" id="diagStatsChip">累计 0 次请求</span>
          <span class="chevron" aria-hidden="true">▾</span>
        </div>
      </div>
      <div class="fold-body">
        <div class="diag-kv-grid">
          <div class="diag-kv"><div class="k">运行与记忆模式</div><div class="v" id="modeVal">-</div></div>
          <div class="diag-kv"><div class="k">浏览器连接通道</div><div class="v" id="transportVal">-</div></div>
          <div class="diag-kv"><div class="k">网页流监听状态</div><div class="v" id="rpcVal">-</div></div>
          <div class="diag-kv"><div class="k">并发 / 排队 / 历史总请求</div><div class="v" id="statsVal">0 / 0 / 0</div></div>
        </div>
        <div class="logs-box" id="logsBox">暂无运行记录</div>
      </div>
    </section>
  </div>

  <!-- Integration Guide Modal -->
  <div class="modal-backdrop" id="guideModal" hidden>
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="guideModalTitle">
      <div class="modal-head">
        <h3 id="guideModalTitle">第三方软件接入指南（一键复制）</h3>
        <button type="button" class="btn sm" id="closeGuideBtn">关闭</button>
      </div>
      <p class="muted" style="margin:0 0 14px;font-size:0.83rem;">
        支持 Codex、Cursor、Cline、ChatBox 等所有兼容 OpenAI 接口的软件接入本机服务：
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
          <div class="copy-val" id="copyOtherModels">gemini-4-argon, gemini-3.1-pro, gemini-3.8-flash</div>
        </div>
        <button type="button" class="btn sm soft" data-copy-target="copyOtherModels">复制</button>
      </div>
      <div class="copy-row" style="margin-bottom:0;">
        <div class="copy-meta">
          <div class="copy-label">API Key（访问密钥）</div>
          <div class="copy-val">本机已设为免密，留空或任意填写均可</div>
        </div>
      </div>
    </div>
  </div>
  <script>
    (function () {
      const baseOrigin = window.location.origin;
      document.getElementById('baseUrlVal').textContent = baseOrigin + '/v1';

      let selectedPeriod = 'all';
      let selectedCurrency = 'CNY';
      try {
        const savedCur = localStorage.getItem('geb_currency');
        if (savedCur === 'USD' || savedCur === 'CNY') selectedCurrency = savedCur;
      } catch (_) {}

      let lastUsageSnapshot = null;
      let lastCtxSnapshot = null;
      let seatIndexByTabId = {};
      let seatIndexBySlotKey = {};
      let selectedSlotKey = null;
      let idleSeatsExpanded = false;
      let enrichedBySlotKey = {};
      let refreshResetTimer = null;
      let lastDistSnapshot = null;
      let lastDefaultModel = 'gemini-4-carbon';
      let distPolicyDirty = false;
      let currentShareKey = '';
      let currentShareBundle = '';

      const PERIOD_LABELS = {
        today: '今日',
        yesterday: '昨日',
        '7d': '近 7 天',
        '30d': '近 30 天',
        session: '本次启动',
        all: '全部累计'
      };

      const TASK_META = {
        responses: { label: 'Codex 编程', color: '#2563eb' },
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

      // Restore collapsible section states
      document.querySelectorAll('.fold-section').forEach(function (sec) {
        const key = sec.getAttribute('data-sec');
        if (!key) return;
        try {
          const saved = localStorage.getItem('geb_fold_' + key);
          if (saved === '1') sec.classList.add('collapsed');
          else if (saved === '0') sec.classList.remove('collapsed');
        } catch (_) {}
      });

      document.querySelectorAll('[data-fold-toggle]').forEach(function (head) {
        head.addEventListener('click', function (ev) {
          if (ev.target && ev.target.closest('#currencySwitch')) return;
          const sec = head.closest('.fold-section');
          if (!sec) return;
          sec.classList.toggle('collapsed');
          const key = sec.getAttribute('data-sec');
          if (key) {
            try {
              localStorage.setItem('geb_fold_' + key, sec.classList.contains('collapsed') ? '1' : '0');
            } catch (_) {}
          }
        });
      });

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
        const v = Math.max(0, Number(n || 0));
        if (v >= 1000000000) return (v / 1000000000).toFixed(2) + 'B';
        if (v >= 1000000) return (v / 1000000).toFixed(1) + 'M';
        if (v >= 1000) return (v / 1000).toFixed(1) + 'K';
        return String(Math.round(v));
      }

      function resolveTokenParts(b) {
        const obj = b || {};
        const cached = Math.max(0, Number(obj.cached_tokens || 0));
        const input = obj.delta_prompt_tokens !== undefined
          ? Math.max(0, Number(obj.delta_prompt_tokens || 0))
          : Math.max(0, Number(obj.prompt_tokens || 0) - cached);
        const output = Math.max(0, Number(obj.completion_tokens || 0));
        const total = input + output + cached;
        return { input: input, output: output, cached: cached, total: total };
      }

      function fmtMoney(usdVal, cnyVal) {
        if (selectedCurrency === 'USD') {
          const u = Number(usdVal || 0);
          if (u <= 0) return '$0.00';
          return u < 0.01 ? ('$' + u.toFixed(4)) : ('$' + u.toFixed(2));
        }
        const c = Number(cnyVal || 0);
        if (c <= 0) return '¥0.00';
        return c < 0.01 ? ('¥' + c.toFixed(3)) : ('¥' + c.toFixed(2));
      }

      function friendlyModel(raw) {
        const s = String(raw || '').toLowerCase();
        if (!s) return 'Carbon';
        if (s.includes('carbon')) return 'Carbon';
        if (s.includes('argon')) return 'Argon';
        if (s.includes('3.1')) return '3.1 Pro';
        if (s.includes('3.8') || s.includes('flash')) return '3.8 Flash';
        if (s.includes('2.5-pro')) return '2.5 Pro';
        if (s.includes('2.5-flash')) return '2.5 Flash';
        if (s.includes('enterprise-web')) return '当前网页模型';
        return String(raw);
      }

      function friendlyStrategyBadge(strategy) {
        const s = String(strategy || '').toLowerCase();
        const span = document.createElement('span');
        if (s.includes('incremental') || s.includes('delta')) {
          span.className = 'chip ok';
          span.textContent = '智能续聊';
        } else if (s.includes('compact') || s.includes('checkpoint')) {
          span.className = 'chip warn';
          span.textContent = '记忆整理';
        } else if (s.includes('bootstrap') || s.includes('reset') || s.includes('new') || s.includes('full')) {
          span.className = 'chip busy';
          span.textContent = '新开对话';
        } else if (s.includes('recover')) {
          span.className = 'chip purple';
          span.textContent = '断点续接';
        } else if (!s || s === '-') {
          span.className = 'chip';
          span.textContent = '常规调用';
        } else {
          span.className = 'chip';
          span.textContent = '单次直发';
        }
        return span;
      }

      function friendlyStrategyLabel(strategy) {
        const s = String(strategy || '').toLowerCase();
        if (!s || s === '-') return '尚未发送对话';
        if (s.includes('incremental') || s.includes('delta')) return '智能增量续聊';
        if (s.includes('compact') || s.includes('checkpoint')) return '自动记忆整理';
        if (s.includes('bootstrap') || s.includes('reset') || s.includes('new') || s.includes('full')) return '新开完整对话';
        if (s.includes('recover')) return '断点恢复续接';
        return '单次直发';
      }

      function shortWorkLabel(phase, waitingCnt) {
        if (phase === 'generating') return '模型回复';
        if (phase === 'running_tool') return '执行命令';
        if (phase === 'waiting_model') return '等待回复';
        if (phase === 'queued' || waitingCnt > 0) return '排队 ' + Math.max(1, waitingCnt);
        return '工作中';
      }

      function friendlySeatLabel(slotKey, tabId) {
        if (slotKey && seatIndexBySlotKey[slotKey]) {
          return seatIndexBySlotKey[slotKey] + ' 号席位';
        }
        if (tabId && seatIndexByTabId[String(tabId)]) {
          return seatIndexByTabId[String(tabId)] + ' 号席位';
        }
        if (tabId) {
          return '席位 #' + String(tabId).slice(-4);
        }
        return '自动席位';
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
        tag.className = 'chip ' + (state === 'ok' ? 'ok' : (state === 'warn' ? 'warn' : ''));
      }

      async function resetSingleSeat(slotKey, seatNum, btn) {
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
          btn.textContent = seatNum + '号已换新 ✓';
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

        document.getElementById('detailSeatBadge').textContent = item.seatNum + ' 号席位';
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

        const srcMap = {
          codex: 'Codex 会话',
          prompt: '首条提问',
          web: '网页标题',
          task: '专项任务'
        };
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
        document.getElementById('detailTurns').textContent =
          uTurns + ' 轮提问 · ' + tSteps + ' 步交互';
        document.getElementById('detailReqs').textContent =
          cmpCnt > 0
            ? ('当前新网页步数 · 本页完成 ' + completedReqs + ' 次')
            : ('本页累计完成 ' + completedReqs + ' 次请求');

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
        const seatNum = item.seatNum;
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
        nameWrap.textContent = seatNum + ' 号席位';

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
        if (s.slot_key) {
          card.setAttribute('data-slot-key', s.slot_key);
        }
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
          const num = idx + 1;
          if (s.slot_key) seatIndexBySlotKey[s.slot_key] = num;
          if (s.tab_id) seatIndexByTabId[String(s.tab_id)] = num;
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
            seatNum: num,
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

        // Auto-sort: working seats at the very top, then active conversations by recency, then standby seats
        enrichedSlots.sort(function (a, b) {
          const rankA = a.isWorking ? 0 : (a.hasActiveConv ? 1 : 2);
          const rankB = b.isWorking ? 0 : (b.hasActiveConv ? 1 : 2);
          if (rankA !== rankB) return rankA - rankB;
          if (rankA === 0) {
            return a.seatNum - b.seatNum;
          }
          if (rankA === 1 && b.lastTs !== a.lastTs) {
            return b.lastTs - a.lastTs;
          }
          return a.seatNum - b.seatNum;
        });

        const activeItems = enrichedSlots.filter(function (x) { return x.isWorking || x.hasActiveConv; });
        const idleItems = enrichedSlots.filter(function (x) { return !x.isWorking && !x.hasActiveConv; });

        if (busyCount > 0) {
          chip.textContent = activeItems.length + ' 活跃 (' + busyCount + ' 工作中) · ' + idleItems.length + ' 空闲';
        } else if (activeItems.length > 0) {
          chip.textContent = activeItems.length + ' 活跃 · ' + idleItems.length + ' 空闲';
        } else {
          chip.textContent = slots.length + ' 个空闲待命';
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
        if (selectedPeriod.startsWith('day:')) {
          const dk = selectedPeriod.slice(4);
          if (daily[dk]) {
            return { group: daily[dk], label: dk, filterDay: dk, mode: 'day' };
          }
        }
        if (periods[selectedPeriod]) {
          let lbl = PERIOD_LABELS[selectedPeriod] || selectedPeriod;
          if (selectedPeriod === 'today' && usage.today_date) lbl = '今日 (' + usage.today_date.slice(5) + ')';
          if (selectedPeriod === 'yesterday' && usage.yesterday_date) lbl = '昨日 (' + usage.yesterday_date.slice(5) + ')';
          return {
            group: periods[selectedPeriod],
            label: lbl,
            filterDay: selectedPeriod === 'today' ? usage.today_date : (selectedPeriod === 'yesterday' ? usage.yesterday_date : null),
            mode: selectedPeriod
          };
        }
        return {
          group: { summary: usage.all_time || {}, by_model: usage.by_model || {}, by_task: usage.by_task || {} },
          label: '全部累计',
          filterDay: null,
          mode: 'all'
        };
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
        days.forEach(function (dk) {
          const s = (dailyMap[dk] && dailyMap[dk].summary) || {};
          const tot = selectedCurrency === 'USD'
            ? Number(s.cached_equivalent_cost_usd || 0)
            : Number(s.cached_equivalent_cost_cny || 0);
          if (tot > maxTotal) maxTotal = tot;
        });

        days.forEach(function (dk) {
          const s = (dailyMap[dk] && dailyMap[dk].summary) || {};
          const actual = selectedCurrency === 'USD'
            ? Number(s.cached_equivalent_cost_usd || 0)
            : Number(s.cached_equivalent_cost_cny || 0);
          const reqCnt = Number(s.requests || 0);
          const actualH = reqCnt > 0 ? Math.max(8, (actual / maxTotal) * 100) : 3;
          const tp = resolveTokenParts(s);

          const col = document.createElement('div');
          col.className = 'bar-col' + (activeFilterDay === dk ? ' selected' : '');
          col.title = reqCnt > 0
            ? (dk + ' · ' + reqCnt + ' 次调用\n等效花费: ' +
              fmtMoney(s.cached_equivalent_cost_usd, s.cached_equivalent_cost_cny) +
              '\n缓存命中率: ' + Number(s.cache_hit_pct || 0).toFixed(1) + '%' +
              '\n累计 Token: ' + fmtLedgerTok(tp.total) +
              '（输入 ' + fmtLedgerTok(tp.input) + ' / 输出 ' + fmtLedgerTok(tp.output) + ' / 缓存 ' + fmtLedgerTok(tp.cached) + '）')
            : (dk + ' · 无调用记录');

          const valTop = document.createElement('span');
          valTop.className = 'bar-val';
          valTop.textContent = reqCnt > 0
            ? fmtMoney(s.cached_equivalent_cost_usd, s.cached_equivalent_cost_cny)
            : '';

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
          empty.style.fontSize = '0.8rem';
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

        const resolved = resolveSelectedGroup(usage);
        const curSummary = (resolved.group && resolved.group.summary) || {};
        const byModel = (resolved.group && resolved.group.by_model) || {};
        const byTask = (resolved.group && resolved.group.by_task) || {};
        const pLabel = resolved.label;
        const ap = usage.active_pricing || {};
        const fx = Number(usage.usd_to_cny_rate || 6.8);

        if (ap.display_name) {
          const rParts = (
            ap.input_rate_per_1m !== undefined &&
            ap.cached_input_rate_per_1m !== undefined &&
            ap.output_rate_per_1m !== undefined
          )
            ? (' ($' + ap.input_rate_per_1m + ' / $' + ap.cached_input_rate_per_1m + ' / $' + ap.output_rate_per_1m + ')')
            : '';
          document.getElementById('ledgerRateBadge').textContent =
            friendlyModel(ap.display_name) + rParts + ' · 汇率 ' + fx;
        }

        document.getElementById('activePeriodTag').textContent = pLabel;

        document.getElementById('ledgerActualMain').textContent =
          fmtMoney(curSummary.cached_equivalent_cost_usd, curSummary.cached_equivalent_cost_cny);
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
        if (cmpCnt > 0 && mediaCnt > 0) {
          callsThirdLabel = '专项 ' + (cmpCnt + mediaCnt);
        } else if (mediaCnt > 0) {
          callsThirdLabel = '媒体 ' + mediaCnt;
        }
        const callsSubText = 'Codex ' + respCnt + ' / 对话 ' + chatCnt + ' / ' + callsThirdLabel;
        const callsFullTip = 'Codex ' + respCnt + ' · 对话 ' + chatCnt + ' · 记忆整理 ' + cmpCnt + (mediaCnt > 0 ? (' · 多媒体 ' + mediaCnt) : '');
        const callsSubEl = document.getElementById('ledgerCallsSub');
        callsSubEl.textContent = callsSubText;
        callsSubEl.title = callsFullTip;

        const sumTok = resolveTokenParts(curSummary);
        const promptTokTotal = sumTok.input + sumTok.cached;
        const hitPct = curSummary.cache_hit_pct !== undefined
          ? Number(curSummary.cache_hit_pct || 0)
          : (promptTokTotal > 0 ? (sumTok.cached / promptTokTotal) * 100 : 0);
        document.getElementById('ledgerCacheMain').textContent = hitPct.toFixed(1) + '%';
        const cacheSubText = '缓存 ' + fmtLedgerTok(sumTok.cached) + ' / 总输入 ' + fmtLedgerTok(promptTokTotal);
        const cacheSubEl = document.getElementById('ledgerCacheSub');
        cacheSubEl.textContent = cacheSubText;
        cacheSubEl.title = '同对话复用缓存 ' + fmtLedgerTok(sumTok.cached) + ' / 新增输入 ' + fmtLedgerTok(sumTok.input) + ' / 总输入上下文 ' + fmtLedgerTok(promptTokTotal);

        document.getElementById('ledgerTokensMain').textContent = fmtLedgerTok(sumTok.total);
        const tokSubText = '输入 ' + fmtLedgerTok(sumTok.input) + ' / 输出 ' + fmtLedgerTok(sumTok.output) + ' / 缓存 ' + fmtLedgerTok(sumTok.cached);
        const tokSubEl = document.getElementById('ledgerTokensSub');
        tokSubEl.textContent = tokSubText;
        tokSubEl.title = tokSubText;

        renderDailyBars(usage, resolved.filterDay);
        renderTaskBreakdown(byTask, reqCount);

        // Model Table
        const modelBody = document.getElementById('ledgerModelBody');
        const modelKeys = Object.keys(byModel);
        if (modelKeys.length === 0) {
          renderEmptyRow(modelBody, 6, '当前时间范围暂无模型记录');
        } else {
          modelBody.replaceChildren();
          modelKeys.forEach(function (mk) {
            const m = byModel[mk] || {};
            const mt = resolveTokenParts(m);
            const tr = document.createElement('tr');
            tr.appendChild(makeCell(m.display_name || friendlyModel(mk)));
            tr.appendChild(makeCell((m.requests || 0) + ' 次', 'num'));
            tr.appendChild(makeCell(fmtLedgerTok(mt.total), 'num'));
            tr.appendChild(makeCell(fmtLedgerTok(mt.input) + ' / ' + fmtLedgerTok(mt.output) + ' / ' + fmtLedgerTok(mt.cached)));
            const mediaParts = [];
            if (m.images_generated) mediaParts.push(m.images_generated + ' 张图');
            if (m.videos_generated) mediaParts.push(m.videos_generated + ' 条视频');
            if (m.audio_requests) mediaParts.push(m.audio_requests + ' 次语音');
            if (m.compactions) mediaParts.push(m.compactions + ' 次整理');
            tr.appendChild(makeCell(mediaParts.length > 0 ? mediaParts.join(' · ') : '-'));
            tr.appendChild(makeCell(fmtMoney(m.cached_equivalent_cost_usd, m.cached_equivalent_cost_cny), 'num'));
            modelBody.appendChild(tr);
          });
        }

        // Daily Table
        const dailyBody = document.getElementById('ledgerDailyBody');
        const dailyMap = usage.daily || {};
        const sortedDays = Object.keys(dailyMap).sort().reverse().slice(0, 30);
        if (sortedDays.length === 0) {
          renderEmptyRow(dailyBody, 6, '暂无按日统计记录');
        } else {
          dailyBody.replaceChildren();
          sortedDays.forEach(function (dk) {
            const ds = (dailyMap[dk] && dailyMap[dk].summary) || {};
            const dt = resolveTokenParts(ds);
            const tr = document.createElement('tr');
            if (resolved.filterDay === dk) tr.style.background = '#eff6ff';
            tr.appendChild(makeCell(dk));
            tr.appendChild(makeCell((ds.requests || 0) + ' 次', 'num'));
            tr.appendChild(makeCell(fmtLedgerTok(dt.total), 'num'));
            tr.appendChild(makeCell(fmtLedgerTok(dt.input) + ' / ' + fmtLedgerTok(dt.output) + ' / ' + fmtLedgerTok(dt.cached)));
            const dParts = [];
            if (ds.compactions) dParts.push(ds.compactions + ' 次整理');
            if (ds.images_generated) dParts.push(ds.images_generated + ' 张图');
            if (ds.videos_generated) dParts.push(ds.videos_generated + ' 条视频');
            if (ds.audio_requests) dParts.push(ds.audio_requests + ' 次语音');
            tr.appendChild(makeCell(dParts.length > 0 ? dParts.join(' · ') : '常规对话与编程'));
            tr.appendChild(makeCell(fmtMoney(ds.cached_equivalent_cost_usd, ds.cached_equivalent_cost_cny), 'num'));
            dailyBody.appendChild(tr);
          });
        }

        // Recent Requests Table (humanized labels)
        const recentBody = document.getElementById('ledgerRecentBody');
        const allRecents = Array.isArray(usage.recent_requests) ? usage.recent_requests : [];
        const startedAt = Number(usage.started_at || 0);
        const todayStr = usage.today_date || '';
        const filteredRecents = allRecents.filter(function (r) {
          if (resolved.filterDay) return r.day === resolved.filterDay;
          if (resolved.mode === 'session') return Number(r.ts || 0) >= startedAt;
          if (resolved.mode === '7d' && todayStr && r.day) {
            const diff = (Date.parse(todayStr) - Date.parse(r.day)) / 86400000;
            return diff >= 0 && diff <= 6;
          }
          if (resolved.mode === '30d' && todayStr && r.day) {
            const diff = (Date.parse(todayStr) - Date.parse(r.day)) / 86400000;
            return diff >= 0 && diff <= 29;
          }
          return true;
        }).slice(0, 15);

        if (filteredRecents.length === 0) {
          renderEmptyRow(recentBody, 7, '当前时间范围暂无调用流水');
        } else {
          recentBody.replaceChildren();
          filteredRecents.forEach(function (r) {
            const tr = document.createElement('tr');
            const tStr = r.ts ? ((r.day ? r.day.slice(5) + ' ' : '') + new Date(r.ts * 1000).toLocaleTimeString()) : '-';
            const tMeta = TASK_META[r.task_type] || { label: r.task_label || '常规调用' };
            const rt = resolveTokenParts(r);
            tr.appendChild(makeCell(tStr));
            if (r.tenant_id && r.tenant_id !== 'host') {
              const taskWrap = document.createElement('span');
              taskWrap.style.display = 'inline-flex';
              taskWrap.style.alignItems = 'center';
              taskWrap.style.gap = '6px';
              taskWrap.appendChild(document.createTextNode(tMeta.label));
              const tBadge = document.createElement('span');
              tBadge.className = 'chip warn';
              const rawTName = String(r.tenant_name || '外部');
              tBadge.textContent = rawTName.startsWith('客户端 · ') ? rawTName.slice(6) : rawTName;
              taskWrap.appendChild(tBadge);
              tr.appendChild(makeCell(taskWrap));
            } else {
              tr.appendChild(makeCell(tMeta.label));
            }
            tr.appendChild(makeCell(friendlyModel(r.display_name || r.model) + ' · ' + friendlySeatLabel(r.slot_key, r.tab_id)));
            tr.appendChild(makeCell(friendlyStrategyBadge(r.strategy)));
            tr.appendChild(makeCell(
              fmtLedgerTok(rt.total) + '（' + fmtLedgerTok(rt.input) + ' / ' + fmtLedgerTok(rt.output) + ' / ' + fmtLedgerTok(rt.cached) + '）'
            ));
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

          // Step 1: Local Service
          setStepState(
            'stepServiceCard', 'stepServiceIcon', 'stepServiceDesc', 'stepServiceTag',
            'ok', '✓', '8000 · ' + defModelName, '在线'
          );

          // Step 2 & 3: Extension & Google Tabs
          const extOnline = clients.length > 0 || slots.length > 0 || Boolean(active);
          const extParallelCap = (active && active.max_parallel_tabs)
            || hub.client_max_parallel_tabs
            || hub.max_parallel_tabs
            || 10;
          const authBtn = document.getElementById('stepAuthBtn');

          const workingTotal = renderSeatsGrid(slots, data.context_monitor) || 0;

          if (!extOnline) {
            setStepState(
              'stepExtCard', 'stepExtIcon', 'stepExtDesc', 'stepExtTag',
              'warn', '!', '请开启浏览器扩展', '待连接'
            );
            setStepState(
              'stepAuthCard', 'stepAuthIcon', 'stepAuthDesc', 'stepAuthTag',
              'warn', '3', '等待插件识别', '待检测'
            );
            authBtn.hidden = false;
            dot.className = 'dot err';
            txt.textContent = '等待插件连接';
          } else if (slots.length === 0) {
            setStepState(
              'stepExtCard', 'stepExtIcon', 'stepExtDesc', 'stepExtTag',
              'ok', '✓', '上限 ' + extParallelCap + ' 路并行', '已连接'
            );
            setStepState(
              'stepAuthCard', 'stepAuthIcon', 'stepAuthDesc', 'stepAuthTag',
              'warn', '!', '请打开并登录网页', '待打开'
            );
            authBtn.hidden = false;
            dot.className = 'dot';
            txt.textContent = '请打开工作页';
          } else {
            setStepState(
              'stepExtCard', 'stepExtIcon', 'stepExtDesc', 'stepExtTag',
              'ok', '✓', '上限 ' + extParallelCap + ' 路并行', '已连接'
            );
            setStepState(
              'stepAuthCard', 'stepAuthIcon', 'stepAuthDesc', 'stepAuthTag',
              'ok', '✓', slots.length + ' 页已登录', '就绪'
            );
            authBtn.hidden = true;
            dot.className = 'dot ok';
            txt.textContent = workingTotal > 0
              ? (workingTotal + ' 工作中 · 共 ' + slots.length + ' 页')
              : (slots.length + ' 页就绪待命');
          }

          if (data.usage) {
            renderLedger(data.usage, data.context_monitor);
          }
          lastDefaultModel = data.default_model || 'gemini-4-carbon';
          if (data.distribution) {
            renderDistribution(data.distribution, lastDefaultModel);
          }

          // Diagnostics drawer
          document.getElementById('modeVal').textContent = '浏览器桥接 · 智能增量记忆';
          document.getElementById('transportVal').textContent =
            extOnline ? ('WebSocket 实时双工 · 上限 ' + extParallelCap + ' 并发') : '未连接';
          document.getElementById('rpcVal').textContent =
            (active && active.captured_rpc) ? '已捕获流式通道' : '页面组件与流式双监听';
          const effBusy = Math.max(Number(hub.working_slots || 0), Number(hub.active_slots_busy || 0), workingTotal);
          document.getElementById('statsVal').textContent =
            effBusy + ' 工作中 / ' + (hub.queued_requests || 0) + ' 排队 / ' + (hub.total_requests || 0) + ' 累计';
          document.getElementById('diagStatsChip').textContent = '累计 ' + (hub.total_requests || 0) + ' 次请求';

          const logs = hub.recent_logs || [];
          const logsBox = document.getElementById('logsBox');
          if (logs.length === 0) {
            logsBox.textContent = '暂无运行记录';
          } else {
            logsBox.replaceChildren();
            logs.slice().reverse().forEach(function (item) {
              const div = document.createElement('div');
              div.className = 'log-item';
              const t = new Date(item.ts * 1000).toLocaleTimeString();
              div.textContent = '[' + t + '] ' + item.message;
              logsBox.appendChild(div);
            });
          }
          refreshOk = true;
        } catch (_) {
          document.getElementById('connDot').className = 'dot err';
          document.getElementById('connText').textContent = '本地服务未响应';
          setStepState(
            'stepServiceCard', 'stepServiceIcon', 'stepServiceDesc', 'stepServiceTag',
            'err', '✕', '8000 未响应', '离线'
          );
        }
        if (manual) {
          const waitMs = Math.max(0, 460 - (performance.now() - t0));
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
                if (!upRes.ok) {
                  throw new Error('附件上传失败：' + (await upRes.text()));
                }
                const upMeta = await upRes.json();
                if (upMeta.category === 'image') {
                  parts.push({ type: 'image_url', image_url: { url: upMeta.id } });
                } else if (upMeta.category === 'audio') {
                  parts.push({ type: 'input_audio', file_id: upMeta.id, filename: upMeta.filename });
                } else if (upMeta.category === 'video') {
                  parts.push({ type: 'video_url', file_id: upMeta.id, filename: upMeta.filename });
                } else {
                  parts.push({ type: 'file', file: { file_id: upMeta.id, filename: upMeta.filename, mime_type: upMeta.mime_type } });
                }
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
            body: JSON.stringify({
              model: chosenModel,
              stream: true,
              messages: [{ role: 'user', content: messageContent }]
            })
          });
          clearTimeout(earlyPollTimer);
          refreshStatus();
          if (!res.ok) {
            throw new Error('服务返回错误 (' + res.status + ')：' + (await res.text()));
          }
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
                if (delta && delta.content) {
                  out.textContent += delta.content;
                }
              } catch (_) {}
            }
          }
          const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
          box.className = 'probe-result ok';
          resTitle.textContent = '✓ 测试通过 · ' + modelName + ' 回复正常';
          resMeta.textContent = '耗时 ' + elapsed + ' 秒';
          resMeta.className = 'chip ok';
          if (!out.textContent.trim()) {
            out.textContent = '（已收到正常完成信号）';
          }
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
      document.getElementById('quickProbeBtn').addEventListener('click', function () {
        executeTestRequest(true);
      });
      document.getElementById('sendBtn').addEventListener('click', function () {
        executeTestRequest(false);
      });
      document.getElementById('refreshBtn').addEventListener('click', function () {
        refreshStatus(true);
      });
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
        resetSingleSeat(item.slot.slot_key, item.seatNum, document.getElementById('detailResetBtn'));
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

      // Public Distribution & Multi-Client Controls
      function buildAdminHeaders() {
        const authVal = document.getElementById('apiKeyInput').value.trim();
        const h = { 'Content-Type': 'application/json' };
        if (authVal) h['Authorization'] = 'Bearer ' + authVal;
        return h;
      }

      function resolveShareApiBase(policyObj) {
        let raw = String((policyObj && policyObj.public_base_url) || window.location.origin || '').trim();
        while (raw.endsWith('/')) raw = raw.slice(0, -1);
        if (!raw.endsWith('/v1')) raw = raw + '/v1';
        return raw;
      }

      function buildClientBundleText(clientObj, policyObj, defModel) {
        const apiBase = resolveShareApiBase(policyObj);
        const recModel = defModel || 'gemini-4-carbon';
        const keyStr = clientObj.api_key || clientObj.key_preview || '';
        const lines = [
          '【' + (clientObj.name || '共享客户端') + ' · AI 接口接入信息】',
          '接口地址 (Base URL): ' + apiBase,
          '专属密钥 (API Key): ' + keyStr,
          '推荐主力模型: ' + recModel,
          '其他可选模型: gemini-4-argon, gemini-3.1-pro, gemini-3.8-flash',
          '使用方法: 打开 Cursor / Codex / Cherry Studio / ChatBox 等任意支持 OpenAI 接口的软件，填入以上接口地址和专属密钥即可直接使用（无需安装任何插件）。'
        ];
        return lines.join('\n');
      }

      function showShareBundle(clientObj) {
        const policyObj = (lastDistSnapshot && lastDistSnapshot.policy) || {};
        currentShareKey = String(clientObj.api_key || '');
        currentShareBundle = buildClientBundleText(clientObj, policyObj, lastDefaultModel);
        document.getElementById('distShareTitle').textContent = '「' + (clientObj.name || '客户端') + '」专属接入包（可直接复制发给对方）';
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

        const sumChip = document.getElementById('distSummaryChip');
        if (!enabled) {
          sumChip.className = 'chip warn';
          sumChip.textContent = '外部接入已暂停 · ' + totalKeys + ' 把密钥';
        } else if (extInf > 0 || extQueued > 0) {
          sumChip.className = 'chip busy';
          sumChip.textContent = '外部 ' + extInf + ' 工作中' + (extQueued > 0 ? (' / ' + extQueued + ' 排队') : '') + ' · ' + activeKeys + ' 把密钥启用';
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
          renderEmptyRow(tbody, 6, '暂未创建外部密钥 · 本机直连无需密钥，如需分享给他人请在上方生成专属密钥');
          return;
        }
        tbody.replaceChildren();
        clientsList.forEach(function (c) {
          const tr = document.createElement('tr');

          // Col 1: Name + Status + Note
          const nameBox = document.createElement('div');
          const nameRow = document.createElement('div');
          nameRow.style.display = 'flex';
          nameRow.style.alignItems = 'center';
          nameRow.style.gap = '6px';
          const strong = document.createElement('strong');
          strong.textContent = c.name || c.client_id;
          const st = document.createElement('span');
          st.className = c.enabled ? 'chip ok' : 'chip warn';
          st.textContent = c.enabled ? '启用中' : '已暂停';
          nameRow.appendChild(strong);
          nameRow.appendChild(st);
          nameBox.appendChild(nameRow);
          if (c.note) {
            const noteSub = document.createElement('div');
            noteSub.className = 'muted';
            noteSub.style.fontSize = '0.73rem';
            noteSub.textContent = c.note;
            nameBox.appendChild(noteSub);
          }
          tr.appendChild(makeCell(nameBox));

          // Col 2: Secret Key preview + Copy buttons
          const keyBox = document.createElement('div');
          keyBox.className = 'dist-row-actions';
          const codeSpan = document.createElement('span');
          codeSpan.className = 'mono';
          codeSpan.style.fontSize = '0.77rem';
          codeSpan.textContent = c.key_preview || 'sk-geb-****';
          keyBox.appendChild(codeSpan);
          if (c.api_key) {
            const cpBundleBtn = document.createElement('button');
            cpBundleBtn.type = 'button';
            cpBundleBtn.className = 'btn sm soft';
            cpBundleBtn.textContent = '复制接入包';
            cpBundleBtn.addEventListener('click', function () {
              showShareBundle(c);
              copyTextWithFeedback(cpBundleBtn, buildClientBundleText(c, policy, defModel), '接入包已复制 ✓');
            });
            const cpKeyBtn = document.createElement('button');
            cpKeyBtn.type = 'button';
            cpKeyBtn.className = 'btn sm';
            cpKeyBtn.textContent = '复制密钥';
            cpKeyBtn.addEventListener('click', function () {
              copyTextWithFeedback(cpKeyBtn, c.api_key, '已复制 ✓');
            });
            keyBox.appendChild(cpBundleBtn);
            keyBox.appendChild(cpKeyBtn);
          }
          tr.appendChild(makeCell(keyBox));

          // Col 3: Real-time concurrency
          const inf = Number(c.in_flight || 0);
          const qd = Number(c.queued || 0);
          const maxC = Number(c.max_concurrency || 1);
          const concChip = document.createElement('span');
          concChip.className = inf > 0 ? 'chip busy' : 'chip';
          concChip.textContent = inf + ' / ' + maxC + ' 并发' + (qd > 0 ? ('（排队 ' + qd + '）') : '');
          tr.appendChild(makeCell(concChip));

          // Col 4: Rate limits & daily quota
          const reqLim = Number(c.daily_request_limit || 0);
          const tokLim = Number(c.daily_token_limit || 0);
          const todayReqs = Number(c.today_requests || 0);
          const todayToks = Number(c.today_tokens || 0);
          const quotaParts = [
            (c.rpm_limit || 30) + ' 次/分',
            '今日 ' + todayReqs + (reqLim > 0 ? ('/' + reqLim) : '') + ' 次'
          ];
          if (tokLim > 0 || todayToks > 0) {
            quotaParts.push(fmtLedgerTok(todayToks) + (tokLim > 0 ? ('/' + fmtLedgerTok(tokLim)) : '') + ' Tok');
          }
          tr.appendChild(makeCell(quotaParts.join(' · ')));

          // Col 5: Cumulative usage & last active
          const totReqs = Number(c.total_requests || 0);
          const totToks = Number(c.total_tokens || 0);
          const lastStr = c.last_used_at ? new Date(c.last_used_at * 1000).toLocaleTimeString() : '尚未使用';
          const statText = totReqs + ' 次 (' + fmtLedgerTok(totToks) + ') · ' + lastStr + (c.last_ip ? (' · ' + c.last_ip) : '');
          tr.appendChild(makeCell(statText));

          // Col 6: Actions (Toggle, Rotate, Revoke)
          const actBox = document.createElement('div');
          actBox.className = 'dist-row-actions';

          const toggleBtn = document.createElement('button');
          toggleBtn.type = 'button';
          toggleBtn.className = 'btn sm';
          toggleBtn.textContent = c.enabled ? '暂停' : '启用';
          toggleBtn.addEventListener('click', async function () {
            toggleBtn.disabled = true;
            try {
              const res = await fetch('/api/distribution/clients/' + encodeURIComponent(c.client_id), {
                method: 'PATCH',
                headers: buildAdminHeaders(),
                body: JSON.stringify({ enabled: !c.enabled })
              });
              const d = await res.json().catch(function () { return {}; });
              if (res.ok && d.distribution) renderDistribution(d.distribution, lastDefaultModel);
            } finally {
              toggleBtn.disabled = false;
            }
          });

          const rotBtn = document.createElement('button');
          rotBtn.type = 'button';
          rotBtn.className = 'btn sm';
          rotBtn.textContent = '换新密钥';
          let rotConfirm = false;
          rotBtn.addEventListener('click', async function () {
            if (!rotConfirm) {
              rotConfirm = true;
              rotBtn.textContent = '确认换新?';
              setTimeout(function () {
                if (rotConfirm) {
                  rotConfirm = false;
                  rotBtn.textContent = '换新密钥';
                }
              }, 3000);
              return;
            }
            rotConfirm = false;
            rotBtn.disabled = true;
            try {
              const res = await fetch('/api/distribution/clients/' + encodeURIComponent(c.client_id) + '/rotate-key', {
                method: 'POST',
                headers: buildAdminHeaders()
              });
              const d = await res.json().catch(function () { return {}; });
              if (res.ok && d.distribution) {
                renderDistribution(d.distribution, lastDefaultModel);
                if (d.client) showShareBundle(d.client);
              }
            } finally {
              rotBtn.disabled = false;
            }
          });

          const delBtn = document.createElement('button');
          delBtn.type = 'button';
          delBtn.className = 'btn sm danger-soft';
          delBtn.textContent = '吊销';
          let delConfirm = false;
          delBtn.addEventListener('click', async function () {
            if (!delConfirm) {
              delConfirm = true;
              delBtn.textContent = '确认吊销?';
              setTimeout(function () {
                if (delConfirm) {
                  delConfirm = false;
                  delBtn.textContent = '吊销';
                }
              }, 3000);
              return;
            }
            delConfirm = false;
            delBtn.disabled = true;
            try {
              const res = await fetch('/api/distribution/clients/' + encodeURIComponent(c.client_id), {
                method: 'DELETE',
                headers: buildAdminHeaders()
              });
              const d = await res.json().catch(function () { return {}; });
              if (res.ok && d.distribution) {
                renderDistribution(d.distribution, lastDefaultModel);
                document.getElementById('distShareBox').hidden = true;
                refreshStatus();
              }
            } finally {
              delBtn.disabled = false;
            }
          });

          actBox.appendChild(toggleBtn);
          actBox.appendChild(rotBtn);
          actBox.appendChild(delBtn);
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
            method: 'PATCH',
            headers: buildAdminHeaders(),
            body: JSON.stringify(payload)
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
            note: document.getElementById('distClientNote').value.trim()
          };
          const res = await fetch('/api/distribution/clients', {
            method: 'POST',
            headers: buildAdminHeaders(),
            body: JSON.stringify(payload)
          });
          const d = await res.json().catch(function () { return {}; });
          if (res.ok && d.ok) {
            nameEl.value = '';
            document.getElementById('distClientNote').value = '';
            if (d.distribution) renderDistribution(d.distribution, lastDefaultModel);
            if (d.client) showShareBundle(d.client);
          }
        } finally {
          btn.disabled = false;
        }
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

      // Guide Modal & One-Click Copy
      const modal = document.getElementById('guideModal');
      document.getElementById('openGuideBtn').addEventListener('click', function () {
        modal.hidden = false;
      });
      document.getElementById('closeGuideBtn').addEventListener('click', function () {
        modal.hidden = true;
      });
      modal.addEventListener('click', function (ev) {
        if (ev.target === modal) modal.hidden = true;
      });
      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && !modal.hidden) modal.hidden = true;
      });

      document.querySelectorAll('[data-copy-target]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          const tid = btn.getAttribute('data-copy-target');
          const el = document.getElementById(tid);
          if (!el) return;
          const text = el.textContent.trim();
          const orig = btn.textContent;
          try {
            await navigator.clipboard.writeText(text);
            btn.textContent = '已复制 ✓';
          } catch (_) {
            btn.textContent = '请手动复制';
          }
          setTimeout(function () { btn.textContent = orig; }, 1400);
        });
      });

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
