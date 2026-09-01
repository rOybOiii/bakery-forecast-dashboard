"""Render the local dashboard and measure the history chart containment.

This is a developer-only visual smoke test. It does not ship with or alter the
dashboard's data contract.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8501")
    parser.add_argument("--restaurant", default="Hurricane")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".visual-tests/history-layout.png"),
    )
    parser.add_argument("--width", type=int, default=1800)
    parser.add_argument("--height", type=int, default=1100)
    parser.add_argument("--max-overflow", type=float, default=0.5)
    parser.add_argument("--exercise-slider", action="store_true")
    parser.add_argument("--drag-steps", type=int, default=12)
    parser.add_argument("--drag-step-delay-ms", type=int, default=30)
    parser.add_argument("--drag-cycles", type=int, default=1)
    parser.add_argument("--exercise-hover", action="store_true")
    parser.add_argument("--exercise-evidence", action="store_true")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        print("browser launched", flush=True)
        page = browser.new_page(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=1,
        )
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(args.url, wait_until="networkidle")
        print("page loaded", flush=True)

        restaurant = page.get_by_role("combobox", name="Restaurant")
        if restaurant.input_value() != args.restaurant:
            restaurant.click()
            page.get_by_text(args.restaurant, exact=True).last.click()
            page.wait_for_timeout(1200)
        print(f"selected {args.restaurant}", flush=True)

        evidence_guide_visible = False
        evidence_guide_text = None
        if args.exercise_evidence:
            page.get_by_text(
                "What do the evidence labels mean?", exact=True
            ).click()
            page.wait_for_timeout(250)
            popover_body = page.locator("[data-testid='stPopoverBody']")
            evidence_guide_visible = bool(
                popover_body.count() and popover_body.last.is_visible()
            )
            if evidence_guide_visible:
                evidence_guide_text = popover_body.last.inner_text()

        expander = page.locator("[data-testid='stExpander']").filter(
            has_text="Explore forecast, event, and sales-shift history"
        ).last
        expander.wait_for(state="visible")
        details = expander.locator("details")
        if details.count() and not details.evaluate("node => node.open"):
            expander.get_by_text(
                "Explore forecast, event, and sales-shift history", exact=True
            ).click()
        page.wait_for_timeout(1800)
        print("opened history", flush=True)

        chart_key = expander.locator("[data-testid='stVegaLiteChart']").last
        chart_key.wait_for(state="visible")

        chart_box = chart_key.bounding_box()
        expander_box = expander.bounding_box()
        plot_box = chart_key.bounding_box()
        rendered_locator = chart_key.locator("canvas, svg").last
        rendered_box = rendered_locator.bounding_box()
        rendered_tag = rendered_locator.evaluate("node => node.tagName")
        result_classes = chart_key.evaluate(
            "node => Array.from(node.parentElement.parentElement.classList)"
        )

        result = {
            "viewport": {"width": args.width, "height": args.height},
            "chart_host": chart_box,
            "vega_host": plot_box,
            "rendered_chart": rendered_box,
            "rendered_tag": rendered_tag,
            "expander": expander_box,
            "chart_ancestor_classes": result_classes,
            "evidence_guide_visible": evidence_guide_visible,
            "evidence_guide_text": evidence_guide_text,
        }
        if rendered_box and expander_box:
            result["right_overflow_px"] = round(
                max(
                    0,
                    rendered_box["x"]
                    + rendered_box["width"]
                    - expander_box["x"]
                    - expander_box["width"],
                ),
                2,
            )

        if args.exercise_slider and plot_box:
            navigator_selection = expander.locator(".history-selection")
            navigator_track = expander.locator(".history-track")
            navigator_selection.wait_for(state="visible")
            selection_box = navigator_selection.bounding_box()
            track_box = navigator_track.bounding_box()
            assert selection_box is not None and track_box is not None
            page.evaluate(
                """node => {
                    const rendered = node.querySelector('svg, canvas');
                    window.__historyLayout = {
                        samples: [],
                        mutations: 0,
                        frameTimes: [],
                        longTasks: [],
                        timer: setInterval(() => {
                            const hostBox = node.getBoundingClientRect();
                            const renderedBox = rendered?.getBoundingClientRect();
                            window.__historyLayout.samples.push({
                                hostHeight: hostBox.height,
                                renderedHeight: renderedBox?.height ?? null,
                                markCount: node.querySelectorAll('path, rect, line').length,
                            });
                        }, 10),
                    };
                    let previousFrame;
                    const sampleFrame = timestamp => {
                        if (previousFrame !== undefined) {
                            window.__historyLayout.frameTimes.push(
                                timestamp - previousFrame
                            );
                        }
                        previousFrame = timestamp;
                        window.__historyLayout.frameRequest =
                            requestAnimationFrame(sampleFrame);
                    };
                    window.__historyLayout.frameRequest =
                        requestAnimationFrame(sampleFrame);
                    if ('PerformanceObserver' in window) {
                        window.__historyLayout.longTaskObserver =
                            new PerformanceObserver(entries => {
                                for (const entry of entries.getEntries()) {
                                    window.__historyLayout.longTasks.push(
                                        entry.duration
                                    );
                                }
                            });
                        try {
                            window.__historyLayout.longTaskObserver.observe({
                                type: 'longtask',
                                buffered: true,
                            });
                        } catch (_) {}
                    }
                    new MutationObserver(records => {
                        window.__historyLayout.mutations += records.length;
                    }).observe(node, {subtree: true, childList: true, attributes: true});
                }""",
                chart_key.element_handle(),
            )
            drag_start_x = selection_box["x"] + selection_box["width"] * 0.5
            drag_end_x = drag_start_x - track_box["width"] * 0.18
            drag_y = selection_box["y"] + selection_box["height"] * 0.5
            result["drag_hit_test"] = page.evaluate(
                """([x, y]) => {
                    const node = document.elementFromPoint(x, y);
                    return node ? {
                        tag: node.tagName,
                        className: node.className,
                        cursor: getComputedStyle(node).cursor,
                    } : null;
                }""",
                [drag_start_x, drag_y],
            )
            page.mouse.move(
                drag_start_x,
                drag_y,
            )
            page.mouse.down()
            for cycle in range(args.drag_cycles):
                cycle_start = drag_start_x if cycle % 2 == 0 else drag_end_x
                cycle_end = drag_end_x if cycle % 2 == 0 else drag_start_x
                for step in range(1, args.drag_steps + 1):
                    fraction = step / args.drag_steps
                    page.mouse.move(
                        cycle_start + (cycle_end - cycle_start) * fraction,
                        drag_y,
                    )
                    page.wait_for_timeout(args.drag_step_delay_ms)
            layout = page.evaluate(
                """() => {
                    clearInterval(window.__historyLayout.timer);
                    cancelAnimationFrame(window.__historyLayout.frameRequest);
                    window.__historyLayout.longTaskObserver?.disconnect();
                    return window.__historyLayout;
                }"""
            )
            page.mouse.up()
            # The navigator commits once on release, which intentionally
            # triggers a single Streamlit rerun of the heavier charts.
            page.wait_for_timeout(1200)
            host_heights = [sample["hostHeight"] for sample in layout["samples"]]
            rendered_heights = [
                sample["renderedHeight"]
                for sample in layout["samples"]
                if sample["renderedHeight"] is not None
            ]
            mark_counts = [sample["markCount"] for sample in layout["samples"]]
            frame_times = layout["frameTimes"]
            sorted_frame_times = sorted(frame_times)
            frame_p95 = (
                sorted_frame_times[
                    min(
                        len(sorted_frame_times) - 1,
                        int(len(sorted_frame_times) * 0.95),
                    )
                ]
                if sorted_frame_times
                else None
            )
            result["layout_during_drag"] = {
                "host_height_min": min(host_heights),
                "host_height_max": max(host_heights),
                "rendered_height_min": min(rendered_heights),
                "rendered_height_max": max(rendered_heights),
                "mark_count_min": min(mark_counts),
                "mark_count_max": max(mark_counts),
                "mutation_batches": layout["mutations"],
                "frame_time_average_ms": (
                    round(sum(frame_times) / len(frame_times), 2)
                    if frame_times
                    else None
                ),
                "frame_time_p95_ms": (
                    round(frame_p95, 2) if frame_p95 is not None else None
                ),
                "frame_time_max_ms": (
                    round(max(frame_times), 2) if frame_times else None
                ),
                "long_task_count": len(layout["longTasks"]),
                "long_task_max_ms": (
                    round(max(layout["longTasks"]), 2)
                    if layout["longTasks"]
                    else None
                ),
            }
            result["slider_exercised"] = True

        if args.exercise_hover and plot_box:
            chart_key.scroll_into_view_if_needed()
            page.wait_for_timeout(150)
            hover_box = chart_key.bounding_box()
            assert hover_box is not None
            page.mouse.move(
                hover_box["x"] + hover_box["width"] * 0.52,
                hover_box["y"] + 190,
            )
            page.wait_for_timeout(500)
            result["hover_hit_test"] = page.evaluate(
                """([x, y]) => {
                    const node = document.elementFromPoint(x, y);
                    return node ? {
                        tag: node.tagName,
                        ariaLabel: node.getAttribute('aria-label'),
                        cursor: getComputedStyle(node).cursor,
                    } : null;
                }""",
                [
                    hover_box["x"] + hover_box["width"] * 0.52,
                    hover_box["y"] + 190,
                ],
            )
            tooltips = page.locator(".vg-tooltip")
            result["tooltip_text"] = (
                tooltips.last.inner_text() if tooltips.count() else None
            )
            result["nearest_day_hover_exercised"] = True

        page.screenshot(path=str(args.output), full_page=True)
        element_output = args.output.with_name(
            f"{args.output.stem}-history{args.output.suffix}"
        )
        expander.screenshot(path=str(element_output))
        print(json.dumps(result, indent=2))
        print(f"screenshot={args.output.resolve()}")
        print(f"history_screenshot={element_output.resolve()}")
        browser.close()

        if page_errors:
            raise SystemExit(f"Browser errors: {page_errors}")
        if result.get("right_overflow_px", 0) > args.max_overflow:
            raise SystemExit(
                f"History chart overflowed by {result['right_overflow_px']} px"
            )


if __name__ == "__main__":
    main()
