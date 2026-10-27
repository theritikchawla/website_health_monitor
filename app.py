"""
app.py - the Streamlit dashboard.   Run it with:   streamlit run app.py

Streamlit re-runs this whole script from top to bottom every time you click
something. That is why the data lives in SQLite (not in variables) and why the
background scheduler is created once with @st.cache_resource.
"""
import re

import plotly.express as px
import streamlit as st

import config
import database
import helpers
import monitor
from scheduler import MonitorScheduler

st.set_page_config(page_title="Website Health Monitor", page_icon="🩺", layout="wide")


@st.cache_resource
def get_scheduler() -> MonitorScheduler:
    """Created once per app process, so there is only ever ONE background checker."""
    database.init_db()
    database.delete_old_checks()  # drop history older than the retention period
    return MonitorScheduler()


def handle_add_website(raw_url: str) -> None:
    """Validate, save and immediately check a new website (messages show in the sidebar)."""
    try:
        url = monitor.validate_url(raw_url)
    except monitor.InvalidURLError as exc:
        st.error(f"Invalid URL: {exc}")
        return
    try:
        added = database.add_website(url)
    except database.WebsiteLimitError as exc:
        st.error(str(exc))
        return
    if not added:
        st.warning("That website is already being monitored.")
        return
    with st.spinner("Running the first check..."):
        database.save_result(monitor.check_website(url))
    st.success(f"Added {url}")


database.init_db()
scheduler = get_scheduler()

# ----------------------------------------------------------------------------
# Sidebar: add / schedule / remove
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("➕ Add a website")
    with st.form("add_website_form", clear_on_submit=True):
        raw_url = st.text_input("Website URL", placeholder="https://example.com")
        submitted = st.form_submit_button("Add website")
    if submitted:
        handle_add_website(raw_url)
    st.caption(f"Up to {config.MAX_WEBSITES} websites. Only http:// and https:// are accepted.")

    st.header("⏱️ Automatic monitoring")
    interval = st.number_input(
        "Check every (seconds)",
        min_value=config.MIN_INTERVAL_SECONDS,
        max_value=config.MAX_INTERVAL_SECONDS,
        value=config.DEFAULT_INTERVAL_SECONDS,
        step=10,
    )
    if scheduler.is_running:
        st.success(f"Running: checking every {scheduler.interval_seconds} s")
        if st.button("Update interval"):
            scheduler.start(int(interval))
            st.rerun()
        if st.button("Stop monitoring"):
            scheduler.stop()
            st.rerun()
    else:
        st.info("Automatic monitoring is stopped.")
        if st.button("Start monitoring", type="primary"):
            scheduler.start(int(interval))
            st.rerun()
    if scheduler.last_run_at:
        st.caption(f"Last check cycle finished: {helpers.format_timestamp(scheduler.last_run_at)} UTC")
    st.caption(
        "⚠️ Automatic checks only run **while this app is running**. "
        "If you close the terminal or the browser tab's server stops, monitoring stops too."
    )

    websites = database.list_websites()
    if websites:
        st.header("🗑️ Remove a website")
        to_remove = st.selectbox("Website to remove", websites, key="remove_select")
        if st.button("Remove (also deletes its history)"):
            database.remove_website(to_remove)
            st.rerun()

# ----------------------------------------------------------------------------
# Main page
# ----------------------------------------------------------------------------
st.title("🩺 Website Health Monitor")
st.write(
    "Checks whether websites are online, how fast they respond and whether they return errors. "
    "Results are stored locally so you can review the history."
)
st.info("Only monitor websites that you own or have permission to check.")

if not database.list_websites():
    st.info("No websites yet. Add your first URL in the sidebar to get started.")
    st.stop()

if st.button("🔄 Run check now", type="primary"):
    with st.spinner("Checking all websites..."):
        checked = scheduler.run_once()
    if checked is None:
        st.warning("A check is already running. Please wait a moment and try again.")
    else:
        st.success(f"Checked {checked} website(s).")


@st.fragment(run_every=config.UI_REFRESH_SECONDS)
def render_dashboard() -> None:
    """Re-runs by itself every few seconds so new automatic results appear."""
    latest = database.get_latest_results()
    sites = latest["url"].tolist()
    if not sites:
        st.info("No websites yet. Add your first URL in the sidebar.")
        return

    # --- summary metrics ---------------------------------------------------
    summary = helpers.compute_summary(latest)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Websites monitored", summary["total"])
    c2.metric("🟢 UP", summary["up"])
    c3.metric("🔴 DOWN", summary["down"])
    c4.metric("🟡 DEGRADED", summary["degraded"])
    avg = summary["avg_response_ms"]
    c5.metric("Avg response time", f"{avg:.0f} ms" if avg is not None else "—")
    if summary["not_checked"]:
        st.caption(f"{summary['not_checked']} website(s) have not been checked yet. Click “Run check now”.")

    # --- status table --------------------------------------------------------
    st.subheader("Current status")
    st.dataframe(
        helpers.prepare_status_table(latest),
        hide_index=True,
        column_config={"Response time (ms)": st.column_config.NumberColumn(format="%.0f")},
    )
    with st.expander("How are statuses decided?"):
        st.markdown(
            f"- 🟢 **UP**: the site answered (HTTP below 400) within {config.DEGRADED_THRESHOLD_MS} ms.\n"
            f"- 🟡 **DEGRADED**: the site is *reachable* but something is off: an HTTP 4xx error "
            f"(for example 404 page not found), a slow response, or a redirect that was not followed for safety.\n"
            f"- 🔴 **DOWN**: the site could not be reached (timeout, DNS, SSL or connection failure), "
            f"or it answered with an HTTP 5xx server error.\n\n"
            f"The **Reachable** column tells these apart: “No” means no HTTP response at all."
        )

    # --- history ---------------------------------------------------------------
    st.subheader("📈 Monitoring history")
    selected = st.selectbox("Choose a website", sites, key="history_site")
    stats = database.get_stats(selected)
    if stats["total_checks"] == 0:
        st.info("No history yet for this website. Click “Run check now” or start automatic monitoring.")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Checks recorded", stats["total_checks"])
    m2.metric("Uptime (not DOWN)", f"{stats['uptime_pct']:.1f} %")
    m3.metric("Avg response time", "—" if stats["avg_response_ms"] is None else f"{stats['avg_response_ms']:.0f} ms")
    m4.metric("Checks with problems", stats["problem_checks"])

    history = database.get_history(selected)
    chart_data = helpers.prepare_history(history)
    tab_chart, tab_failures, tab_all = st.tabs(["Response time chart", "Previous failures", "All checks"])

    with tab_chart:
        if chart_data["response_time_ms"].notna().any():
            fig = px.line(
                chart_data, x="checked_at_dt", y="response_time_ms", markers=True,
                labels={"checked_at_dt": "Time (UTC)", "response_time_ms": "Response time (ms)"},
            )
            fig.add_hline(y=config.DEGRADED_THRESHOLD_MS, line_dash="dash", annotation_text="Degraded threshold")
            fig.update_layout(height=360, margin=dict(l=10, r=10, t=30, b=10))
            st.plotly_chart(fig)
            st.caption("Gaps in the line are checks where the site did not respond at all.")
        else:
            st.info("The site has not responded yet, so there is no response time to chart.")

    with tab_failures:
        failures = database.get_failures(selected)
        if failures.empty:
            st.success("No failures recorded for this website. 🎉")
        else:
            st.dataframe(helpers.history_table(failures), hide_index=True)

    with tab_all:
        table = helpers.history_table(history)
        st.dataframe(table, hide_index=True)
        safe_name = re.sub(r"[^A-Za-z0-9.-]+", "_", selected).strip("_")
        st.download_button(
            "⬇️ Download as CSV",
            data=table.to_csv(index=False).encode("utf-8"),
            file_name=f"history_{safe_name}.csv",
            mime="text/csv",
        )
    st.caption(f"Showing the latest {config.HISTORY_ROW_LIMIT} checks; history older than {config.RETENTION_DAYS} days is deleted automatically.")


render_dashboard()
