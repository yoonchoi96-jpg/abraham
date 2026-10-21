"""Deterministic, zero-network synthetic LSH retrieval benchmark."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time

import numpy as np

from scripts.reader_novelty import NoveltyIndex, POLICY_REVISION
from scripts.reader_crosslingual import band_keys


def run(docs=10000, trials=5):
    if not 100<=docs<=100000 or not 1<=trials<=100:
        raise ValueError("BOUNDED_BENCHMARK_REQUIRED")
    db=sqlite3.connect(":memory:")
    idx=NoveltyIndex(db,model_id="synthetic-lsh-model")
    rng=np.random.default_rng(1024)
    dims=32
    rows=[]
    bands=[]
    vector0=np.zeros(dims,dtype=np.float32)
    vector0[0]=1.0
    for n in range(docs):
        if n==docs//2 or n==docs//2+1:
            vector=vector0.copy()
        else:
            vector=rng.normal(size=dims).astype(np.float32)
            vector/=np.linalg.norm(vector)
        key=f"document-{n}"
        rows.append((
            key,"synthetic-lsh-model",hashlib.sha256(key.encode()).hexdigest(),
            f"Independent headline {n}","Independently generated paragraph",
            "[]",vector.astype("<f4").tobytes(),dims,"novel",None,None,None,
            "BENCHMARK_ONLY",POLICY_REVISION,None, f"independent headline {n}",
        ))
        bands.extend([(key,"synthetic-lsh-model",POLICY_REVISION,bucket_idx,bucket)
                      for bucket_idx,bucket in band_keys(vector)])
    db.executemany("""INSERT INTO novelty_v1
        (document_key,model_id,body_sha256,title,body_excerpt,markers,
        vector,dimensions,decision,match_key,similarity,coverage,reason,
        policy_revision,match_body_sha256,title_key)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",rows)
    db.executemany("""INSERT INTO novelty_review_bands
        (document_key,model_id,policy_revision,band,bucket)
        VALUES(?,?,?,?,?)""",bands)
    db.commit()
    query=f"document-{docs//2+1}"
    qvec=vector0
    def exhaustive():
        got=[]
        for key,raw in db.execute("""SELECT document_key,vector
                FROM novelty_v1 WHERE model_id=? AND policy_revision=?
                 AND decision='novel' AND document_key<>?""",
                ("synthetic-lsh-model",POLICY_REVISION,query)):
            cosine=float(np.dot(qvec,np.frombuffer(raw,dtype="<f4")))
            if cosine>=0.99:
                got.append(key)
        return got
    expected=exhaustive()
    assert expected==[f"document-{docs//2}"]
    found=idx.cross_language_review(query,limit=5)
    assert any(c["document_key"]==expected[0] for c in found["candidates"])
    times=[]
    for _ in range(trials):
        t0=time.perf_counter()
        exhaustive()
        t1=time.perf_counter()
        idx.cross_language_review(query,limit=5)
        t2=time.perf_counter()
        times.append((t1-t0,t2-t1))
    median=lambda seq:float(np.median(seq))*1000
    old_ms=median([a for a,b in times])
    lsh_ms=median([b for a,b in times])
    result={
        "synthetic_docs":docs,"trials":trials,
        "full_scan_retrieval_median_ms":round(old_ms,3),
        "advisory_lsh_retrieval_median_ms":round(lsh_ms,3),
        "observed_lookup_speedup":round(old_ms/max(lsh_ms,1e-9),2),
        "known_exact_vector_neighbor_found":True,
        "approximate_recall_not_proven":True,
        "deepseek_api_calls":0,"automatic_api_suppression":False,
    }
    db.close()
    return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--docs",type=int,default=10000)
    p.add_argument("--trials",type=int,default=5)
    a=p.parse_args()
    print(json.dumps(run(a.docs,a.trials),sort_keys=True))


if __name__=="__main__":
    main()
