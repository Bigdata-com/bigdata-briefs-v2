"""Email-safe HTML for brief digests (inline CSS, no interactive web chrome)."""

from __future__ import annotations

import html
from datetime import datetime

from bigdata_briefs.notifications.email_digest import BriefDigest, DigestBullet, DigestEntity


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _format_window(start: datetime | None, end: datetime | None) -> str:
    if start is None and end is None:
        return ""
    if start is not None and end is not None:
        return f"{start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC"
    if start is not None:
        return f"from {start.strftime('%Y-%m-%d %H:%M')} UTC"
    assert end is not None
    return f"until {end.strftime('%Y-%m-%d %H:%M')} UTC"


def _render_citations(bullet: DigestBullet) -> str:
    if not bullet.citations:
        return ""
    parts: list[str] = []
    for citation in bullet.citations[:3]:
        label = citation.source_name or citation.headline or "Source"
        if citation.headline and citation.source_name:
            label = f"{citation.source_name} — {citation.headline}"
        elif citation.headline:
            label = citation.headline
        label_esc = _esc(label)
        if citation.url:
            url_esc = _esc(citation.url)
            parts.append(
                f'<a href="{url_esc}" style="color:#1a56db;text-decoration:none;">{label_esc}</a>'
            )
        else:
            parts.append(label_esc)
    return (
        '<div style="margin:4px 0 0 0;font-size:12px;line-height:1.4;color:#64748b;">'
        + " · ".join(parts)
        + "</div>"
    )


def _render_entity(entity: DigestEntity) -> str:
    name = _esc(entity.entity_name or entity.entity_id)
    eid = _esc(entity.entity_id)
    header = (
        f'<tr><td style="padding:20px 0 8px 0;border-top:1px solid #e2e8f0;">'
        f'<div style="font-size:17px;font-weight:700;color:#0f172a;">{name}</div>'
        f'<div style="font-size:12px;color:#94a3b8;margin-top:2px;">{eid}</div>'
        f"</td></tr>"
    )

    if entity.error:
        err = _esc(entity.error)
        body = (
            f'<tr><td style="padding:0 0 8px 0;">'
            f'<p style="margin:0;padding:10px 12px;background:#fef2f2;border-radius:6px;'
            f'color:#991b1b;font-size:13px;">Run failed: {err}</p>'
            f"</td></tr>"
        )
        return header + body

    if not entity.bullets:
        body = (
            f'<tr><td style="padding:0 0 8px 0;">'
            f'<p style="margin:0;color:#64748b;font-size:14px;font-style:italic;">'
            f"No material developments.</p>"
            f"</td></tr>"
        )
        return header + body

    items: list[str] = []
    for idx, bullet in enumerate(entity.bullets, start=1):
        text = _esc(bullet.text)
        cites = _render_citations(bullet)
        items.append(
            f'<tr><td style="padding:6px 0 6px 0;vertical-align:top;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f"<tr>"
            f'<td style="width:24px;vertical-align:top;font-size:14px;font-weight:600;'
            f'color:#475569;">{idx}.</td>'
            f'<td style="font-size:14px;line-height:1.5;color:#1e293b;">{text}{cites}</td>'
            f"</tr></table></td></tr>"
        )
    return header + "".join(items)


def build_email_html(digest: BriefDigest) -> str:
    """Render a self-contained HTML email body for ``digest``."""
    with_dev, empty, failed = digest.counts()
    window_label = _format_window(digest.window_start, digest.window_end)
    summary_bits = [
        f"{with_dev} with developments",
        f"{empty} empty",
    ]
    if failed:
        summary_bits.append(f"{failed} failed")
    summary = " · ".join(summary_bits)

    entity_rows = "".join(_render_entity(entity) for entity in digest.entities)
    if not entity_rows:
        entity_rows = (
            '<tr><td style="padding:16px 0;color:#64748b;font-size:14px;">'
            "No entities in this run.</td></tr>"
        )

    window_html = ""
    if window_label:
        window_html = (
            f'<p style="margin:0 0 4px 0;font-size:13px;color:#64748b;">{_esc(window_label)}</p>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;
                    overflow:hidden;border:1px solid #e2e8f0;">
        <tr><td style="padding:22px 24px 16px 24px;background:#0f172a;">
          <div style="font-size:11px;letter-spacing:0.08em;text-transform:uppercase;
                      color:#94a3b8;font-weight:600;">Bigdata Briefs</div>
          <div style="margin-top:6px;font-size:22px;font-weight:700;color:#ffffff;">
            Your briefs are ready
          </div>
        </td></tr>
        <tr><td style="padding:18px 24px 8px 24px;">
          {window_html}
          <p style="margin:0;font-size:13px;color:#475569;">{_esc(summary)}</p>
        </td></tr>
        <tr><td style="padding:0 24px 8px 24px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            {entity_rows}
          </table>
        </td></tr>
        <tr><td style="padding:16px 24px 22px 24px;border-top:1px solid #e2e8f0;">
          <p style="margin:0;font-size:11px;line-height:1.5;color:#94a3b8;">
            Generated by Bigdata Briefs. This is an automated notification.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""
