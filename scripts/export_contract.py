"""Export versioned schemas and illustrative renderer fixtures. No network calls."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models.response import AnswerResponse
from app.models.errors import ErrorResponse
from app.models.plan import QueryPlan
from app.models.request import QueryRequest
from app.analysis.models import AnalysisPlan
from app.viz.contract import check_response

ROOT = Path(__file__).resolve().parents[1]


def export():
    for name, model in [("answer",AnswerResponse),("error",ErrorResponse),("request",QueryRequest),("analysis",AnalysisPlan),("plan",QueryPlan)]:
        (ROOT / "docs" / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(),indent=2)+"\n")
    citation = {"nct_id":"NCT00000001","brief_title":"Illustrative trial","field_path":"protocolSection.identificationModule.nctId",
                "field_value":"NCT00000001","evidence_kind":"field_value","source_url":"https://clinicaltrials.gov/study/NCT00000001"}
    # Fixtures illustrate the wire contract; these counts are not clinical results.
    row = {"label":"Example category","value":1,"citations":[citation],"citation_total":1,"citations_truncated":False}
    axis = {"x":{"field":"label","type":"nominal","axis_title":"Category","sort":"none"},"y":{"field":"value","type":"quantitative","unit":"trials","axis_title":"Number of Trials"},"orientation":"horizontal","tooltip":["label","value"]}
    examples = {
        "bar_chart": (axis,[row]),
        "stat": ({"label":{"field":"label","type":"nominal"},"value":{"field":"value","type":"quantitative","unit":"trials"}},[row]),
        "grouped_bar": ({**axis,"series":{"field":"series","type":"nominal"},"stack":False},[{**row,"series":"Example series"}]),
        "stacked_bar": ({**axis,"series":{"field":"series","type":"nominal"},"stack":"normalize"},[{**row,"series":"Example series"}]),
        "time_series": ({"x":{"field":"bucket","type":"temporal","granularity":"year","axis_title":"Start Year"},"y":axis["y"],"point_markers":True},[{**row,"label":"2025","bucket":"2025"}]),
        "pie_chart": ({"theta":{"field":"value","type":"quantitative","unit":"trials"},"color":{"field":"label","type":"nominal"},"total":2},[{**row,"share":.5},{**row,"label":"Second category","share":.5}]),
        "scatter_plot": ({"x":{"field":"x","type":"quantitative","unit":"participants"},"y":{"field":"y","type":"quantitative","unit":"months"},"tooltip":["trial_id","x","y"]},[{"trial_id":"NCT00000001","label":"Illustrative trial","x":100,"y":12,"citations":[citation]}]),
        "network_graph": ({"node":{"id_field":"id","label_field":"label","group_field":"group","size_field":"size","level_field":"level"},"edge":{"source_field":"source","target_field":"target","weight_field":"weight"},"layout_hint":"hierarchical"},{"nodes":[{"id":"a","label":"Subject","group":"subject","size":1,"level":0},{"id":"b","label":"Trial","group":"trial","size":1,"level":1}],"edges":[{"source":"a","target":"b","weight":1,"citations":[citation]}]}),
        "none": ({},[]),
    }
    for kind,(encoding,data) in examples.items():
        body = {"visualization":{"type":kind,"title":"Illustrative renderer fixture","subtitle":"Synthetic example; not clinical findings.","encoding":encoding,"data":data},
                "meta":{"query_interpretation":{"model_interpretation":"Illustrate the response schema."},"intent":"distribution","unit":"trials",
                    "invariant":{"kind":"multi_label","statement":"Example only; values may overlap."},
                    "coverage":{"total_matching_studies":1,"records_fetched":1,"records_analyzed":1,"is_sample":False,"coverage_ratio":1,"sampling_method":"census","sampling_bias_risk":"none","fetch_cap":5000,"pages_fetched":1},
                    "answer_text":"No matching records." if kind=="none" else None}}
        response=AnswerResponse.model_validate(body)
        check_response(response)
        (ROOT/"docs"/"examples"/f"{kind}.json").write_text(response.model_dump_json(indent=2,exclude_none=True)+"\n")

if __name__ == "__main__":
    export()
