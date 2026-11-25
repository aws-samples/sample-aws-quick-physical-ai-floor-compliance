"""Quick Sight dataset and dashboard definitions (boto3 request bodies).

`dashboard.sh` sends these through the QuickSight API. `python scripts/quicksight_defs.py`
exports them to quick/dataset-definition.json and quick/dashboard-definition.json
for review; placeholders in the JSON are filled by scripts/floor.py at run time.
"""
from __future__ import annotations

import json
from pathlib import Path

FINDINGS_COLUMNS = [
    ("unit_id", "STRING"), ("station", "STRING"), ("worker", "STRING"), ("date", "STRING"),
    ("shift", "STRING"), ("start_ts", "STRING"), ("end_ts", "STRING"), ("cycle_seconds", "DECIMAL"),
    ("steps_matched", "INTEGER"), ("steps_total", "INTEGER"), ("deviation_count", "INTEGER"),
    ("high_count", "INTEGER"), ("medium_count", "INTEGER"), ("low_count", "INTEGER"),
    ("compliant", "STRING"), ("procedure_id", "STRING"),
]
DEVIATIONS_COLUMNS = [
    ("deviation_id", "STRING"), ("unit_id", "STRING"), ("station", "STRING"), ("worker", "STRING"),
    ("date", "STRING"), ("shift", "STRING"), ("step_id", "STRING"), ("step_name", "STRING"),
    ("type", "STRING"), ("severity", "STRING"), ("detail", "STRING"), ("observed_seconds", "STRING"),
    ("target_seconds", "STRING"), ("clip_ref", "STRING"), ("ts", "STRING"),
]

DATASET_ACTIONS = [
    "quicksight:UpdateDataSetPermissions", "quicksight:DescribeDataSet", "quicksight:DescribeDataSetPermissions",
    "quicksight:PassDataSet", "quicksight:DescribeIngestion", "quicksight:ListIngestions",
    "quicksight:UpdateDataSet", "quicksight:DeleteDataSet", "quicksight:CreateIngestion", "quicksight:CancelIngestion",
]
DATASOURCE_ACTIONS = [
    "quicksight:UpdateDataSourcePermissions", "quicksight:DescribeDataSource", "quicksight:DescribeDataSourcePermissions",
    "quicksight:PassDataSource", "quicksight:UpdateDataSource", "quicksight:DeleteDataSource",
]
DASHBOARD_ACTIONS = [
    "quicksight:DescribeDashboard", "quicksight:ListDashboardVersions", "quicksight:UpdateDashboardPermissions",
    "quicksight:QueryDashboard", "quicksight:UpdateDashboard", "quicksight:DeleteDashboard",
    "quicksight:DescribeDashboardPermissions", "quicksight:UpdateDashboardPublishedVersion",
]


def data_source(account: str, ds_id: str, workgroup: str, principal: str) -> dict:
    return {
        "AwsAccountId": account, "DataSourceId": ds_id, "Name": "Floor compliance (Athena)",
        "Type": "ATHENA", "DataSourceParameters": {"AthenaParameters": {"WorkGroup": workgroup}},
        "Permissions": [{"Principal": principal, "Actions": DATASOURCE_ACTIONS}],
    }


def _dataset(account: str, ds_arn: str, set_id: str, name: str, table: str, schema: str,
             columns: list[tuple[str, str]], principal: str, transforms: list | None = None) -> dict:
    logical = {"Alias": table, "Source": {"PhysicalTableId": table}}
    if transforms:
        logical["DataTransforms"] = transforms
    return {
        "AwsAccountId": account, "DataSetId": set_id, "Name": name, "ImportMode": "DIRECT_QUERY",
        "PhysicalTableMap": {table: {"RelationalTable": {
            "DataSourceArn": ds_arn, "Catalog": "AwsDataCatalog", "Schema": schema, "Name": table,
            "InputColumns": [{"Name": n, "Type": t} for n, t in columns]}}},
        "LogicalTableMap": {f"{table}-logical": logical},   # keys allow letters, digits and hyphens only
        "Permissions": [{"Principal": principal, "Actions": DATASET_ACTIONS}],
    }


def findings_dataset(account, ds_arn, schema, principal, set_id="floor-compliance-findings"):
    transforms = [{"CreateColumnsOperation": {"Columns": [{
        "ColumnName": "compliant_pct", "ColumnId": "compliant_pct",
        "Expression": "ifelse({compliant} = 'true', 100, 0)"}]}}]
    return _dataset(account, ds_arn, set_id, "Floor compliance: units", "findings", schema,
                    FINDINGS_COLUMNS, principal, transforms)


def deviations_dataset(account, ds_arn, schema, principal, set_id="floor-compliance-deviations"):
    return _dataset(account, ds_arn, set_id, "Floor compliance: deviations", "deviations", schema,
                    DEVIATIONS_COLUMNS, principal)


def _dim(ds: str, col: str, fid: str) -> dict:
    return {"CategoricalDimensionField": {"FieldId": fid, "Column": {"DataSetIdentifier": ds, "ColumnName": col}}}


def _count(ds: str, col: str, fid: str) -> dict:
    return {"CategoricalMeasureField": {"FieldId": fid, "Column": {"DataSetIdentifier": ds, "ColumnName": col},
                                        "AggregationFunction": "COUNT"}}


def _title(text: str) -> dict:
    return {"Visibility": "VISIBLE", "FormatText": {"PlainText": text}}


def dashboard(account: str, dashboard_id: str, findings_arn: str, deviations_arn: str, principal: str) -> dict:
    visuals = [
        {"KPIVisual": {
            "VisualId": "kpi_compliance", "Title": _title("Compliance % (units with no high or medium deviation)"),
            "ChartConfiguration": {"FieldWells": {"Values": [{"NumericalMeasureField": {
                "FieldId": "compliance_avg",
                "Column": {"DataSetIdentifier": "findings", "ColumnName": "compliant_pct"},
                "AggregationFunction": {"SimpleNumericalAggregation": "AVERAGE"}}}]}}}},
        {"BarChartVisual": {
            "VisualId": "bar_station", "Title": _title("Deviations by station and severity"),
            "ChartConfiguration": {
                "FieldWells": {"BarChartAggregatedFieldWells": {
                    "Category": [_dim("deviations", "station", "station")],
                    "Values": [_count("deviations", "deviation_id", "dev_count")],
                    "Colors": [_dim("deviations", "severity", "severity")]}},
                "Orientation": "VERTICAL", "BarsArrangement": "STACKED"}}},
        {"BarChartVisual": {
            "VisualId": "bar_type_shift", "Title": _title("Deviations by type and shift"),
            "ChartConfiguration": {
                "FieldWells": {"BarChartAggregatedFieldWells": {
                    "Category": [_dim("deviations", "type", "dtype")],
                    "Values": [_count("deviations", "deviation_id", "dev_count2")],
                    "Colors": [_dim("deviations", "shift", "shift")]}},
                "Orientation": "HORIZONTAL", "BarsArrangement": "CLUSTERED"}}},
        {"LineChartVisual": {
            "VisualId": "line_trend", "Title": _title("Deviations per day"),
            "ChartConfiguration": {"FieldWells": {"LineChartAggregatedFieldWells": {
                "Category": [_dim("deviations", "date", "date")],
                "Values": [_count("deviations", "deviation_id", "dev_count3")],
                "Colors": [_dim("deviations", "severity", "severity2")]}}}}},
        {"TableVisual": {
            "VisualId": "tbl_deviations", "Title": _title("Deviation log (newest first)"),
            "ChartConfiguration": {
                "FieldWells": {"TableAggregatedFieldWells": {"GroupBy": [
                    _dim("deviations", "ts", "t_ts"), _dim("deviations", "station", "t_station"),
                    _dim("deviations", "worker", "t_worker"), _dim("deviations", "type", "t_type"),
                    _dim("deviations", "severity", "t_sev"), _dim("deviations", "step_name", "t_step"),
                    _dim("deviations", "detail", "t_detail"), _dim("deviations", "deviation_id", "t_id")]}},
                "SortConfiguration": {"RowSort": [{"FieldSort": {"FieldId": "t_ts", "Direction": "DESC"}}]}}}},
    ]
    layout = {"Configuration": {"GridLayout": {"Elements": [
        {"ElementId": "kpi_compliance", "ElementType": "VISUAL", "ColumnIndex": 0, "ColumnSpan": 12, "RowIndex": 0, "RowSpan": 6},
        {"ElementId": "bar_station", "ElementType": "VISUAL", "ColumnIndex": 12, "ColumnSpan": 24, "RowIndex": 0, "RowSpan": 12},
        {"ElementId": "bar_type_shift", "ElementType": "VISUAL", "ColumnIndex": 0, "ColumnSpan": 12, "RowIndex": 6, "RowSpan": 6},
        {"ElementId": "line_trend", "ElementType": "VISUAL", "ColumnIndex": 0, "ColumnSpan": 36, "RowIndex": 12, "RowSpan": 9},
        {"ElementId": "tbl_deviations", "ElementType": "VISUAL", "ColumnIndex": 0, "ColumnSpan": 36, "RowIndex": 21, "RowSpan": 12},
    ]}}}
    return {
        "AwsAccountId": account, "DashboardId": dashboard_id, "Name": "Floor compliance",
        "Definition": {
            "DataSetIdentifierDeclarations": [
                {"Identifier": "findings", "DataSetArn": findings_arn},
                {"Identifier": "deviations", "DataSetArn": deviations_arn}],
            "Sheets": [{"SheetId": "overview", "Name": "Floor compliance", "Visuals": visuals, "Layouts": [layout]}],
        },
        "Permissions": [{"Principal": principal, "Actions": DASHBOARD_ACTIONS}],
    }


def export(out_dir: str | Path) -> None:
    out = Path(out_dir)
    acct, p = "<account-id>", "<quicksight-user-arn>"
    ds = "<athena-data-source-arn>"
    datasets = {
        "data_source": data_source(acct, "floor-compliance-athena", "<athena-workgroup>", p),
        "findings": findings_dataset(acct, ds, "<glue-database>", p),
        "deviations": deviations_dataset(acct, ds, "<glue-database>", p),
    }
    (out / "dataset-definition.json").write_text(json.dumps(datasets, indent=2) + "\n", encoding="utf-8")
    dash = dashboard(acct, "floor-compliance", "<findings-dataset-arn>", "<deviations-dataset-arn>", p)
    (out / "dashboard-definition.json").write_text(json.dumps(dash, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    export(Path(__file__).resolve().parents[1] / "quick")
    print("exported quick/dataset-definition.json and quick/dashboard-definition.json")
