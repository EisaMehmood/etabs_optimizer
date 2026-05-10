import csv
from pathlib import Path

from etabs import CONFIG, EtabsClient


def join_values(values):
    return ";".join(f"{value:.6f}" for value in values)


def make_beam_record(client, frame_name):
    """Export ETABS beam summary values and the kg conversion side by side."""
    res = client.SapModel.DesignConcrete.GetSummaryResultsBeam(frame_name)
    length_mm = client.get_frame_length(frame_name) * 1000.0
    density_kg_mm3 = client.config["STEEL_DENSITY_KG_M3"] / 1e9

    if res[-1] != 0 or res[0] <= 0:
        return {
            "frame": frame_name,
            "kind": "beam",
            "length_mm": length_mm,
            "station_count": 0,
            "summary_mode": "bottom_only",
            "summary_values_mm2": "",
            "summary_max_mm2": 0.0,
            "formula_used": "max(bottom_area_mm2) * length_mm * density_kg_mm3",
            "density_kg_mm3": density_kg_mm3,
            "formula_weight_kg": 0.0,
            "status": "no_results",
        }

    bottom_areas = client._coerce_float_list(res[6] if len(res) > 6 else [])
    max_bottom = max(bottom_areas) if bottom_areas else 0.0

    return {
        "frame": frame_name,
        "kind": "beam",
        "length_mm": length_mm,
        "station_count": len(bottom_areas),
        "summary_mode": "bottom_only",
        "summary_values_mm2": join_values(bottom_areas),
        "summary_max_mm2": max_bottom,
        "formula_used": "max(bottom_area_mm2) * length_mm * density_kg_mm3",
        "density_kg_mm3": density_kg_mm3,
        "formula_weight_kg": max_bottom * length_mm * density_kg_mm3,
        "status": "ok" if bottom_areas else "empty_bottom_area",
    }


def make_column_record(client, frame_name):
    """Export ETABS column summary values and the kg conversion side by side."""
    res = client.SapModel.DesignConcrete.GetSummaryResultsColumn(frame_name)
    length_mm = client.get_frame_length(frame_name) * 1000.0
    density_kg_mm3 = client.config["STEEL_DENSITY_KG_M3"] / 1e9

    if res[-1] != 0 or res[0] <= 0:
        return {
            "frame": frame_name,
            "kind": "column",
            "length_mm": length_mm,
            "station_count": 0,
            "summary_mode": "longitudinal_only",
            "summary_values_mm2": "",
            "summary_max_mm2": 0.0,
            "formula_used": "max(longitudinal_area_mm2) * length_mm * density_kg_mm3",
            "density_kg_mm3": density_kg_mm3,
            "formula_weight_kg": 0.0,
            "status": "no_results",
        }

    longitudinal_areas = client._coerce_float_list(res[5] if len(res) > 5 else [])
    max_longitudinal = max(longitudinal_areas) if longitudinal_areas else 0.0

    return {
        "frame": frame_name,
        "kind": "column",
        "length_mm": length_mm,
        "station_count": len(longitudinal_areas),
        "summary_mode": "longitudinal_only",
        "summary_values_mm2": join_values(longitudinal_areas),
        "summary_max_mm2": max_longitudinal,
        "formula_used": "max(longitudinal_area_mm2) * length_mm * density_kg_mm3",
        "density_kg_mm3": density_kg_mm3,
        "formula_weight_kg": max_longitudinal * length_mm * density_kg_mm3,
        "status": "ok" if longitudinal_areas else "empty_longitudinal_area",
    }


def write_csv(path, records):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "frame",
                "kind",
                "length_mm",
                "station_count",
                "summary_mode",
                "summary_values_mm2",
                "summary_max_mm2",
                "formula_used",
                "density_kg_mm3",
                "formula_weight_kg",
                "status",
            ],
        )
        writer.writeheader()
        writer.writerows(records)


def write_text_summary(path, records, client):
    beam_records = [r for r in records if r["kind"] == "beam"]
    col_records = [r for r in records if r["kind"] == "column"]
    beam_total = sum(r["formula_weight_kg"] for r in beam_records)
    col_total = sum(r["formula_weight_kg"] for r in col_records)
    total = beam_total + col_total

    lines = [
        f"Working model: {client.model_path}",
        f"Design code: {client.design_code}",
        f"Beam count: {len(beam_records)}",
        f"Column count: {len(col_records)}",
        "",
        "Summary values are ETABS summary result areas in mm^2.",
        "Formula values are calculated in kg using the script's density conversion.",
        "",
        f"Beam total kg: {beam_total:.3f}",
        f"Column total kg: {col_total:.3f}",
        f"All members total kg: {total:.3f}",
        "",
        "Formula reference:",
        "  Beams:   max(bottom_area_mm2) * length_mm * density_kg_mm3",
        "  Columns: max(longitudinal_area_mm2) * length_mm * density_kg_mm3",
        f"  density_kg_mm3 = {client.config['STEEL_DENSITY_KG_M3']} / 1e9 = {client.config['STEEL_DENSITY_KG_M3'] / 1e9}",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    config = CONFIG.copy()
    client = EtabsClient(config)
    client.connect()

    model_to_open = client.prepare_model_copy(config["BASE_EDB"])
    if not client.open_model(model_to_open):
        raise RuntimeError(f"Could not open ETABS model: {model_to_open}")

    client.scan_frames()
    client.calculate_total_lengths()

    if not client.run_analysis_design():
        raise RuntimeError("ETABS analysis/design failed.")

    records = []
    for beam in client.beams:
        records.append(make_beam_record(client, beam))
    for column in client.columns:
        records.append(make_column_record(client, column))

    csv_path = Path("rebar_values_all_members.csv")
    txt_path = Path("rebar_values_summary.txt")
    write_csv(csv_path, records)
    write_text_summary(txt_path, records, client)

    beam_total = sum(r["formula_weight_kg"] for r in records if r["kind"] == "beam")
    col_total = sum(r["formula_weight_kg"] for r in records if r["kind"] == "column")
    total = beam_total + col_total

    print(f"Working model: {client.model_path}")
    print(f"Beam count: {len(client.beams)}")
    print(f"Column count: {len(client.columns)}")
    print(f"Beam total kg: {beam_total:.3f}")
    print(f"Column total kg: {col_total:.3f}")
    print(f"All members total kg: {total:.3f}")
    print(f"CSV written to: {csv_path.resolve()}")
    print(f"Text summary written to: {txt_path.resolve()}")


if __name__ == "__main__":
    main()
