"""Offline HTML extraction and disk-backed approximate duplicate candidates.

MinHash is advisory. SHA-256 remains exact; originals and near matches survive.
Index v1: Unicode character 5-grams, 128 permutations, seed 1, 32x4 bands.
"""
import hashlib
import re
import unicodedata


def extract_html(body):
    from trafilatura import extract
    try:
        result = extract(body, favor_precision=True, include_comments=False,
                         include_tables=True, output_format="txt")
    except Exception:
        raise ValueError("HTML_EXTRACTION_FAILED") from None
    if not result or not result.strip():
        raise ValueError("HTML_EXTRACTION_FAILED")
    return result


class NearIndex:
    """SQLite LSH bands avoid keeping the entire Library in Python memory.

    A soft candidate threshold, not a calibrated duplicate decision. Short
    documents (<40 normalized chars) are deliberately exact-only.
    """
    def __init__(self, db, threshold=0.85):
        self.db, self.threshold = db, threshold
        db.executescript("""
        CREATE TABLE IF NOT EXISTS near_v1(body_hash TEXT PRIMARY KEY, signature BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS bands_v1(band INTEGER, bucket BLOB, body_hash TEXT,
            PRIMARY KEY(band,bucket,body_hash));
        """)

    def add(self, body_hash, body):
        from datasketch import MinHash
        import numpy as np
        normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", body).casefold()).strip()
        if len(normalized) < 40:
            return []
        sketch = MinHash(num_perm=128, seed=1)
        # Stream shingles rather than constructing a body-sized Python set.
        seen = set()
        for i in range(len(normalized)-4):
            shingle = normalized[i:i+5]
            if shingle not in seen:
                sketch.update(shingle.encode())
                if len(seen) >= 8192:
                    seen.clear()
                seen.add(shingle)
        bands = [(i, sketch.hashvalues[i*4:(i+1)*4].astype("<u8").tobytes()) for i in range(32)]
        # SQL UNION avoids materializing all candidate hashes at once.
        query = " UNION ".join("SELECT body_hash FROM bands_v1 WHERE band=? AND bucket=?" for _ in bands)
        matches = []
        for (other,) in self.db.execute(query, [v for pair in bands for v in pair]):
            if other == body_hash:
                continue
            raw = self.db.execute("SELECT signature FROM near_v1 WHERE body_hash=?", (other,)).fetchone()[0]
            score = float((sketch.hashvalues == np.frombuffer(raw, dtype="<u8")).mean())
            if score >= self.threshold:
                matches.append({"body_sha256": other, "similarity_estimate": score})
        self.db.execute("INSERT OR IGNORE INTO near_v1 VALUES(?,?)", (body_hash, sketch.hashvalues.astype("<u8").tobytes()))
        self.db.executemany("INSERT OR IGNORE INTO bands_v1 VALUES(?,?,?)", [(i,b,body_hash) for i,b in bands])
        # Bound record growth for huge duplicate clusters; never merge/delete.
        return sorted(matches, key=lambda x: (-x["similarity_estimate"], x["body_sha256"]))[:20]
