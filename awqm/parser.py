"""Strict test-contract parser. This is not a confirmed ArubaOS output format."""
import json
from .model import Radio, number

def normalized_snapshot(text):
    data=json.loads(text)
    if not isinstance(data,dict) or data.get("schema")!="awqm.synthetic.radio.v1":
        raise ValueError("unsupported fixture format; live CLI parser not validated")
    rows=data.get("rows")
    if not isinstance(rows,list) or data.get("complete") is not True or data.get("count")!=len(rows):
        raise ValueError("incomplete snapshot")
    result=[]
    for r in rows:
        row=Radio(**r)
        if row.source!="synthetic" or not row.radio_id or not row.ap_id or not row.controller:
            raise ValueError("invalid provenance")
        if not isinstance(row.observed_at,int) or row.observed_at<0:
            raise ValueError("invalid observation time")
        for v,lo,hi in ((row.utilization,0,100),(row.interference,0,100),(row.noise,-130,0)):
            if v is not None and number(v,lo,hi) is None:
                raise ValueError("invalid measurement")
        result.append(row)
    if len({r.radio_id for r in result})!=len(result):
        raise ValueError("duplicate radio")
    return result
