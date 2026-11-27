"""The dashboard of ``ldp dashboard``, a plain marimo notebook.

``ldp dashboard`` runs it with ``marimo run`` and ``ldp dashboard --edit`` opens a copy of it in the marimo editor. It
lays out what :mod:`local_data_platform.dashboard.service` gives, one tab for each page.

It reads what it was asked for from the environment variable ``LDP_DASHBOARD_SETTINGS``, which ``ldp dashboard``
sets, so start it with that command. A page works from a few plain values, ``selected_table``, ``sql_to_run``,
``report_choice`` and ``window_choice``, and ``write_requested`` and ``write_path``, which the controls set, and
``App.run(defs={...})`` can set them for a test.
"""

import marimo

__generated_with = "0.26.0"
app = marimo.App(width="full", app_title="Local Data Platform")


@app.cell
def _():
    import marimo as mo
    import altair as alt

    from local_data_platform.dashboard import service

    return alt, mo, service


@app.cell
def _(mo, service):
    # One set of tools for the session. What stops it from starting is the page, so that it is read in the browser.
    try:
        settings = service.Settings.from_environment()
        dash = service.Dashboard.open(settings)
        _failure = None
    except Exception as _error:  # noqa: BLE001 - whatever it is, say it on the page
        settings, dash, _failure = None, None, str(_error)
    mo.stop(_failure is not None, mo.callout(mo.md(f"**The dashboard could not start.** {_failure}"), kind="danger"))
    return dash, settings


@app.cell
def _(dash):
    tables = dash.tables()
    return (tables,)


@app.cell
def _(dash, mo, service, settings, tables):
    heading = mo.vstack([
        mo.md("# Local Data Platform"),
        mo.md(f"{service.sources_text(settings, len(tables))}. Every query runs through the tools of `ldp mcp`, with "
              f"at most {dash.limits['max_rows']} rows and {dash.limits['timeout_s']:g} seconds, and is audited with a "
              f"tool name that starts with `dashboard.`"),
    ])
    return (heading,)


# ------------------------------------------------------------------------------------------------------ Tables


@app.cell
def _(mo, tables):
    table_picker = mo.ui.dropdown(options=[item["table"] for item in tables], value=tables[0]["table"], label="Table")
    return (table_picker,)


@app.cell
def _(table_picker):
    selected_table = table_picker.value
    return (selected_table,)


@app.cell
def _(dash, selected_table):
    table_view = dash.table_view(selected_table)
    return (table_view,)


@app.cell
def _(mo, service, table_picker, table_view, tables):
    def _grid(rows, empty):
        if not rows:
            return mo.md(f"_{empty}_")
        return mo.ui.table(rows, selection=None, page_size=10, show_download=False)

    _sample = table_view.sample.rows if table_view.sample is not None else []
    tables_page = mo.vstack([
        mo.md("### Tables"),
        mo.ui.table(service.overview_rows(tables), selection=None, pagination=False, show_download=False),
        table_picker,
        mo.md(f"#### {table_view.table}"),
        _grid(service.facts_rows(table_view.facts), "Nothing is known about this table."),
        mo.md("#### Schema"),
        _grid(table_view.schema, "The schema could not be read."),
        mo.md(f"#### First {len(_sample)} rows"),
        _grid(_sample, "The table has no rows, or they could not be read."),
        mo.md("#### Snapshots, the newest first"),
        _grid(table_view.history, "The table has no snapshot."),
        *[mo.callout(mo.md(line), kind="warn") for line in table_view.problems],
    ])
    return (tables_page,)


# ------------------------------------------------------------------------------------------------------ SQL


@app.cell
def _(dash, mo):
    # A form, so that the query runs when it is sent and the last result stays while the next one is written.
    sql_form = mo.ui.form(mo.ui.code_editor(value=dash.default_sql(), language="sql", min_height=120),
                          submit_button_label="Run the query", bordered=False)
    return (sql_form,)


@app.cell
def _(sql_form):
    sql_to_run = sql_form.value
    return (sql_to_run,)


@app.cell
def _(dash, sql_to_run):
    sql_run = dash.run_sql(sql_to_run) if sql_to_run else None
    return (sql_run,)


@app.cell
def _(mo, service, sql_run):
    # The controls of the quick chart, made again for each result, with the columns that it has.
    _axes = service.chart_axes(sql_run.grid) if sql_run is not None and sql_run.ok else None
    chart_kind = mo.ui.dropdown(options=["bar", "line", "none"], value=_axes.kind if _axes else "none", label="Chart")
    chart_x = mo.ui.dropdown(options=list(_axes.x) if _axes else [], value=_axes.x_default if _axes else None,
                             label="Horizontal")
    chart_y = mo.ui.dropdown(options=list(_axes.y) if _axes else [], value=_axes.y_default if _axes else None,
                             label="Vertical")
    return chart_kind, chart_x, chart_y


@app.cell
def _(alt, chart_kind, chart_x, chart_y, mo, service, sql_run):
    chart = None
    if sql_run is not None and sql_run.ok and chart_kind.value != "none" and chart_x.value and chart_y.value:
        _rows = service.chart_rows(sql_run.grid, chart_x.value, chart_y.value)
        _base = alt.Chart(alt.Data(values=_rows))
        _mark = _base.mark_line(point=True) if chart_kind.value == "line" else _base.mark_bar()
        chart = _mark.encode(
            x=alt.X("x", type=service.chart_x_type(sql_run.grid, chart_x.value, chart_kind.value), title=chart_x.value,
                    sort=None),
            y=alt.Y("y", type="quantitative", title=chart_y.value)).properties(width=640, height=320)
    _no_chart = None
    if sql_run is not None and sql_run.ok and not chart_y.options:
        _no_chart = mo.md("_The result has no number to chart._")
    chart_view = chart if chart is not None else _no_chart
    return chart, chart_view


@app.cell
def _(chart_kind, chart_view, chart_x, chart_y, mo, sql_form, sql_run):
    if sql_run is None:
        _result = mo.md("Write one `SELECT` or `WITH` query and press **Run the query**. The tables are named as the "
                        "Tables page says, in SQL. The result stays until the next query is run.")
    elif not sql_run.ok:
        _result = mo.callout(mo.md(f"**The query did not run** ({sql_run.status}). {sql_run.message}"), kind="danger")
    else:
        _grid = sql_run.grid
        _cut = f", cut at the row cap of {_grid.max_rows}" if _grid.truncated else ""
        _result = mo.vstack([
            mo.md(f"{len(_grid.rows)} rows in {_grid.duration_ms:.0f} ms{_cut}."),
            mo.ui.table(_grid.rows, selection=None, page_size=10) if _grid.rows else mo.md("_The query gave no rows._"),
            mo.hstack([chart_kind, chart_x, chart_y], justify="start"),
            chart_view if chart_view is not None else mo.md(""),
        ])
    sql_page = mo.vstack([mo.md("### SQL"), sql_form, _result])
    return (sql_page,)


# ------------------------------------------------------------------------------------------------------ Reports


@app.cell
def _(dash, mo, service):
    try:
        report_items = dash.reports()
        reports_problem = None
    except Exception as _error:  # noqa: BLE001 - a folder that is not there is a line on the page
        report_items, reports_problem = [], str(_error)
    report_picker = mo.ui.dropdown(options={item.label: item.path for item in report_items}, value=None, label="Report")
    window_picker = mo.ui.dropdown(options=["no window", *service.WINDOWS], value="yesterday", label="Window")
    window_text = mo.ui.text(placeholder="START/END, such as 2026-07-31/2026-08-01", label="or a window of your own",
                             full_width=True)
    return report_items, report_picker, reports_problem, window_picker, window_text


@app.cell
def _(report_picker, window_picker, window_text):
    report_choice = report_picker.value
    window_choice = window_text.value.strip() or ("" if window_picker.value == "no window" else window_picker.value)
    return report_choice, window_choice


@app.cell
def _(dash, report_choice, window_choice):
    report_result = dash.preview_report(report_choice, window_choice) if report_choice else None
    return (report_result,)


@app.cell
def _(mo, report_choice, report_items, service):
    _item = next((item for item in report_items if item.path == report_choice), None)
    out_text = mo.ui.text(value=service.default_output(_item), label="Write the page to", full_width=True)
    write_button = mo.ui.run_button(label="Write the HTML file")
    return out_text, write_button


@app.cell
def _(out_text, write_button):
    write_requested = bool(write_button.value)
    write_path = out_text.value.strip()
    return write_path, write_requested


@app.cell
def _(dash, report_choice, window_choice, write_path, write_requested):
    write_result = (dash.write_report(report_choice, window_choice, write_path)
                    if write_requested and report_choice and write_path else None)
    return (write_result,)


@app.cell
def _(mo, out_text, report_picker, report_result, reports_problem, service, window_picker, window_text, write_button,
      write_result, report_items):
    if reports_problem:
        _body = mo.callout(mo.md(f"**The reports could not be listed.** {reports_problem}"), kind="danger")
    elif not report_items:
        _body = mo.md("There is no report to show. Start the dashboard with `--reports DIR`, a folder of report files.")
    elif report_result is None:
        _body = mo.md("Pick a report. It is built for the window, as `ldp report build` builds it, and shown here.")
    else:
        _parts = []
        if report_result.problems:
            _kind = "warn" if report_result.held else "danger"
            _title = "The report was held back." if report_result.held else "The report was not built."
            _lines = "\n".join(f"- {line}" for line in report_result.problems)
            _parts.append(mo.callout(mo.md(f"**{_title}**\n\n{_lines}"), kind=_kind))
        else:
            _parts.append(mo.ui.table(service.facts_rows(report_result.facts), selection=None, pagination=False,
                                      show_download=False))
            _parts.append(mo.iframe(report_result.html, height="760px"))
            _parts.append(mo.hstack([out_text, write_button], justify="start", align="end"))
        if write_result is not None:
            if write_result.ok and write_result.output:
                _parts.append(mo.callout(mo.md(f"Wrote {write_result.bytes} bytes to `{write_result.output}`, "
                                               f"sha256 `{write_result.sha256}`."), kind="success"))
            else:
                _lines = "\n".join(f"- {line}" for line in write_result.problems)
                _parts.append(mo.callout(mo.md(f"**The file was not written.**\n\n{_lines}"), kind="danger"))
        _body = mo.vstack(_parts)
    reports_page = mo.vstack([mo.md("### Reports"), mo.hstack([report_picker, window_picker], justify="start"),
                              window_text, _body])
    return (reports_page,)


# ------------------------------------------------------------------------------------------------------ Health


@app.cell
def _(dash, mo):
    health = dash.health_rows()
    _counts = ", ".join(f"{count} {name}" for name, count in health.counts.items())
    _many = f"{len(health.rows)} pipeline{'s' if len(health.rows) != 1 else ''}"
    health_page = mo.vstack([
        mo.md("### Health"),
        mo.md(f"{_many}{', ' + _counts if _counts else ''}. The rows of `ldp status`, the worst first."),
        *[mo.callout(mo.md(line), kind="warn") for line in health.problems],
        mo.ui.table(health.rows, selection=None, page_size=15, show_download=False) if health.rows else mo.md(""),
    ])
    return health, health_page


# ------------------------------------------------------------------------------------------------------ the page


@app.cell
def _(health_page, heading, mo, reports_page, sql_page, tables_page):
    mo.vstack([heading, mo.ui.tabs({"Tables": tables_page, "SQL": sql_page, "Reports": reports_page,
                                    "Health": health_page})])
    return


if __name__ == "__main__":
    app.run()
