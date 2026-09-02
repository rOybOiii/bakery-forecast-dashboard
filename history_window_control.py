"""A responsive fixed-width history navigator for the manager dashboard."""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st


_HTML = """
<div class="history-window-control">
  <div class="selection-label selection-label-start"></div>
  <div class="selection-label selection-label-end"></div>
  <div class="selection-label selection-label-range"></div>
  <div class="history-track">
    <div
      class="history-selection"
      role="slider"
      aria-label="Visible history window"
      aria-valuemin="0"
      aria-valuemax="100"
      tabindex="0"
    ></div>
  </div>
  <div class="history-ticks" aria-hidden="true"></div>
</div>
"""


_CSS = """
:host {
  display: block;
  width: 100%;
  color: var(--st-text-color);
  font-family: var(--st-font);
}

.history-window-control {
  box-sizing: border-box;
  position: relative;
  width: 100%;
  height: 72px;
  padding: 20px 10px 0;
  user-select: none;
  -webkit-user-select: none;
}

.history-track {
  position: relative;
  width: 100%;
  height: 18px;
  cursor: default;
}

.history-track::before {
  content: "";
  position: absolute;
  top: 8px;
  right: 0;
  left: 0;
  height: 3px;
  border-radius: 999px;
  background: #768078;
  opacity: 0.55;
}

.history-selection {
  box-sizing: border-box;
  position: absolute;
  top: 0;
  height: 18px;
  min-width: 28px;
  border: 1.5px solid #2f6b55;
  border-radius: 2px;
  background: rgba(47, 107, 85, 0.17);
  cursor: grab;
  touch-action: none;
  will-change: transform;
}

.history-selection:active {
  cursor: grabbing;
  background: rgba(47, 107, 85, 0.23);
}

.history-selection:focus-visible {
  outline: 2px solid #2f6b55;
  outline-offset: 3px;
}

.selection-label {
  position: absolute;
  top: 0;
  z-index: 1;
  color: #1d2a24;
  font-size: 11px;
  font-weight: 650;
  line-height: 16px;
  white-space: nowrap;
  pointer-events: none;
}

.selection-label-start {
  transform: translateX(0);
}

.selection-label-end {
  transform: translateX(-100%);
}

.selection-label-range {
  display: none;
}

.history-window-control.is-compact {
  height: 96px;
  padding-top: 48px;
}

.history-window-control.is-compact .selection-label-start,
.history-window-control.is-compact .selection-label-end {
  display: none;
}

.history-window-control.is-compact .selection-label-range {
  display: block;
  top: 0;
  left: 50%;
  width: max-content;
  transform: translateX(-50%);
  text-align: center;
  line-height: 13px;
  white-space: pre-line;
}

.history-ticks {
  position: relative;
  width: 100%;
  height: 22px;
  margin-top: 7px;
  color: #768078;
  font-size: 11px;
  line-height: 15px;
  pointer-events: none;
}

.history-tick {
  position: absolute;
  white-space: nowrap;
  transform: translateX(-50%);
}

.history-tick:first-child {
  transform: none;
}

.history-tick:last-child {
  transform: translateX(-100%);
}
"""


_JS = """
export default function(component) {
  const { data, parentElement, setStateValue } = component;
  const root = parentElement.querySelector('.history-window-control');
  const track = root.querySelector('.history-track');
  const selection = root.querySelector('.history-selection');
  const startLabel = root.querySelector('.selection-label-start');
  const endLabel = root.querySelector('.selection-label-end');
  const rangeLabel = root.querySelector('.selection-label-range');
  const ticks = root.querySelector('.history-ticks');

  root.classList.toggle('is-compact', Boolean(data.compact));

  const minDay = Number(data.min_day);
  const maxDay = Number(data.max_day);
  const windowDays = Math.max(1, Number(data.window_days));
  const totalDays = Math.max(windowDays, maxDay - minDay);
  const maxStart = Math.max(minDay, maxDay - windowDays);
  let currentStart = Math.min(maxStart, Math.max(minDay, Number(data.start_day)));
  let lastPointerX = null;
  let activePointer = null;
  let frameRequest = null;

  const clamp = value => Math.min(maxStart, Math.max(minDay, value));
  const formatDate = day => new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: '2-digit',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Math.round(day) * 86400000));

  const render = () => {
    frameRequest = null;
    const left = ((currentStart - minDay) / totalDays) * 100;
    const width = (windowDays / totalDays) * 100;
    selection.style.transform = `translateX(${left / Math.max(0.0001, width) * 100}%)`;
    selection.style.width = `${width}%`;
    selection.setAttribute('aria-valuenow', String(Math.round(currentStart)));
    selection.setAttribute(
      'aria-valuetext',
      `${formatDate(currentStart)} to ${formatDate(currentStart + windowDays)}`,
    );
    startLabel.style.left = `calc(10px + (100% - 20px) * ${left / 100})`;
    endLabel.style.left = `calc(10px + (100% - 20px) * ${(left + width) / 100})`;
    startLabel.textContent = formatDate(currentStart);
    endLabel.textContent = formatDate(currentStart + windowDays);
    rangeLabel.textContent = `${formatDate(currentStart)}\n–\n${formatDate(currentStart + windowDays)}`;
  };

  const requestRender = () => {
    if (frameRequest === null) frameRequest = requestAnimationFrame(render);
  };

  const commit = () => {
    currentStart = Math.round(clamp(currentStart));
    render();
    setStateValue('start_day', currentStart);
  };

  const pointerMove = event => {
    if (event.pointerId !== activePointer || lastPointerX === null) return;
    const trackWidth = Math.max(1, track.getBoundingClientRect().width);
    const deltaX = event.clientX - lastPointerX;
    lastPointerX = event.clientX;
    currentStart = clamp(currentStart + (deltaX / trackWidth) * totalDays);
    requestRender();
  };

  const finishPointer = event => {
    if (event.pointerId !== activePointer) return;
    if (selection.hasPointerCapture(event.pointerId)) {
      selection.releasePointerCapture(event.pointerId);
    }
    activePointer = null;
    lastPointerX = null;
    commit();
  };

  selection.onpointerdown = event => {
    if (event.button !== 0) return;
    event.preventDefault();
    activePointer = event.pointerId;
    lastPointerX = event.clientX;
    selection.setPointerCapture(event.pointerId);
  };
  selection.onpointermove = pointerMove;
  selection.onpointerup = finishPointer;
  selection.onpointercancel = finishPointer;
  selection.onkeydown = event => {
    const step = event.shiftKey ? 28 : 7;
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      currentStart = clamp(currentStart - step);
      commit();
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      currentStart = clamp(currentStart + step);
      commit();
    }
  };

  const drawTicks = () => {
    const width = Math.max(1, track.getBoundingClientRect().width);
    const count = Math.max(3, Math.min(11, Math.floor(width / 95)));
    ticks.replaceChildren();
    for (let index = 0; index < count; index += 1) {
      const fraction = index / (count - 1);
      const tick = document.createElement('span');
      tick.className = 'history-tick';
      tick.style.left = `${fraction * 100}%`;
      tick.textContent = new Intl.DateTimeFormat('en-US', {
        month: 'short',
        year: 'numeric',
        timeZone: 'UTC',
      }).format(new Date(Math.round(minDay + fraction * totalDays) * 86400000));
      ticks.appendChild(tick);
    }
  };

  const resizeObserver = new ResizeObserver(() => {
    drawTicks();
    render();
  });
  resizeObserver.observe(track);
  drawTicks();
  render();

  return () => {
    resizeObserver.disconnect();
    if (frameRequest !== null) cancelAnimationFrame(frameRequest);
  };
}
"""


_history_window_component = st.components.v2.component(
    "bakery_history_window",
    html=_HTML,
    css=_CSS,
    js=_JS,
)


_EPOCH = date(1970, 1, 1)


def _day_number(value: date) -> int:
    return (value - _EPOCH).days


def history_window_control(
    *,
    minimum: date,
    maximum: date,
    initial_start: date,
    window_days: int,
    key: str,
    compact: bool = False,
) -> tuple[date, date]:
    """Return a fixed history window, committing changes only after drag release."""

    minimum_day = _day_number(minimum)
    maximum_day = _day_number(maximum)
    maximum_start = max(minimum_day, maximum_day - window_days)
    default_start = min(maximum_start, max(minimum_day, _day_number(initial_start)))
    stored_state = st.session_state.get(key, {})
    stored_start = (
        stored_state.get("start_day", default_start)
        if isinstance(stored_state, dict)
        else default_start
    )
    selected_start = min(maximum_start, max(minimum_day, int(stored_start)))

    result = _history_window_component(
        data={
            "min_day": minimum_day,
            "max_day": maximum_day,
            "window_days": window_days,
            "start_day": selected_start,
            "compact": compact,
        },
        default={"start_day": selected_start},
        key=key,
        on_start_day_change=lambda: None,
        width="stretch",
        height=96 if compact else 72,
    )
    if result.start_day is not None:
        selected_start = min(
            maximum_start,
            max(minimum_day, int(result.start_day)),
        )

    start = _EPOCH + timedelta(days=selected_start)
    end = min(maximum, start + timedelta(days=window_days))
    return start, end
