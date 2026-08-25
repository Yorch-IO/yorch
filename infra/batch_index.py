import os
import time
import shutil
import json
import subprocess
import requests
import fitz
import sys

LIB_DIR = "/home/kheiron/yorch/docaget/libros"
INBOX_DIR = "/home/kheiron/yorch/infra/workspace/inbox/libros"
RUNS_DIR = "/home/kheiron/yorch/infra/workspace/runs"
JSONL_PATH = os.path.join(RUNS_DIR, "indexacion.jsonl")
SUMMARY_PATH = os.path.join(RUNS_DIR, "indexacion-resumen.md")
API_URL = "http://127.0.0.1:8787"

os.makedirs(INBOX_DIR, exist_ok=True)
os.makedirs(RUNS_DIR, exist_ok=True)

def get_awaiting_approval_count():
    try:
        res = subprocess.run(
            ['docker', 'exec', 'company-brain-postgres-1', 'psql', '-U', 'brain', '-d', 'brain', '-tAc', "SELECT count(*) FROM run WHERE state = 'awaiting_approval';"],
            capture_output=True, text=True, check=True
        )
        return int(res.stdout.strip())
    except Exception as e:
        print(f"Warning: could not get awaiting_approval count: {e}")
        return None

def get_indexed_titles():
    res = subprocess.run(
        ['docker', 'exec', 'company-brain-postgres-1', 'psql', '-U', 'brain', '-d', 'brain', '-tAc', 'SELECT title FROM document;'],
        capture_output=True, text=True, check=True
    )
    return set(line.strip() for line in res.stdout.splitlines() if line.strip())

def get_run_state(run_id):
    res = subprocess.run(
        ['docker', 'exec', 'company-brain-postgres-1', 'psql', '-U', 'brain', '-d', 'brain', '-tAc', f"SELECT state FROM run WHERE id='{run_id}';"],
        capture_output=True, text=True
    )
    return res.stdout.strip()

def get_run_cost(run_id):
    res = subprocess.run(
        ['docker', 'exec', 'company-brain-postgres-1', 'psql', '-U', 'brain', '-d', 'brain', '-tAc', f"SELECT sum(usd) FROM cost_entry WHERE run_id='{run_id}';"],
        capture_output=True, text=True
    )
    val = res.stdout.strip()
    try:
        return float(val) if val and val != '' else None
    except (ValueError, TypeError):
        return None

def append_jsonl(record):
    with open(JSONL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def process_file(f):
    title = f[:-4]
    source_key = f"libros/{f}"
    pdf_path = os.path.join(LIB_DIR, f)

    indexed_titles = get_indexed_titles()
    if title in indexed_titles:
        print(f"[{f}] Already indexed. Recording skipped.")
        try:
            with fitz.open(pdf_path) as doc:
                chars = sum(len(p.get_text()) for p in doc)
        except Exception:
            chars = 0
        rec = {
            "archivo": f, "source_key": source_key,
            "run_id": None, "estado": "skipped",
            "caracteres": chars, "chunks": None,
            "estimado_usd": None, "estimado_alto_usd": None, "facturado_usd": None,
            "conceptos": None, "claims": None, "claims_verificados": None,
            "saltado": "ya indexado", "error": None
        }
        append_jsonl(rec)
        return "skipped"

    # Check image-only
    try:
        with fitz.open(pdf_path) as doc:
            num_pages = len(doc)
            total_chars = sum(len(page.get_text()) for page in doc)
            avg_chars = total_chars / num_pages if num_pages > 0 else 0
    except Exception as e:
        print(f"[{f}] Error reading PDF with fitz: {e}")
        reason = f"Error reading PDF: {str(e)}"
        if avg_chars < 300 or "image" in str(e).lower() or num_pages > 0 and total_chars / num_pages < 300:
            reason = f"PDF es solo imagen (< 300 caracteres por pagina)"
        rec = {
            "archivo": f, "source_key": source_key,
            "run_id": None, "estado": "skipped",
            "caracteres": 0, "chunks": None,
            "estimado_usd": None, "estimado_alto_usd": None, "facturado_usd": None,
            "conceptos": None, "claims": None, "claims_verificados": None,
            "saltado": reason, "error": None
        }
        append_jsonl(rec)
        return "skipped"

    if avg_chars < 300:
        reason = f"PDF es solo imagen ({avg_chars:.1f} caracteres por pagina, < 300)"
        print(f"[{f}] Skipping: {reason}")
        rec = {
            "archivo": f, "source_key": source_key,
            "run_id": None, "estado": "skipped",
            "caracteres": total_chars, "chunks": None,
            "estimado_usd": None, "estimado_alto_usd": None, "facturado_usd": None,
            "conceptos": None, "claims": None, "claims_verificados": None,
            "saltado": reason, "error": None
        }
        append_jsonl(rec)
        return "skipped"

    # Copy to inbox
    dest_path = os.path.join(INBOX_DIR, f)
    shutil.copy2(pdf_path, dest_path)

    ingest_payload = {
        "request": {
            "library_id": "lib_teologia",
            "source_path": f"/workspace/inbox/libros/{f}",
            "source_key": source_key,
            "title": title
        },
        "options": {
            "correct": False, "embed": True, "extract_semantics": True,
            "generate_evalset": False, "learn_profile": True, "ignore_profile": False,
            "review_correction": False, "condense_descriptions": False
        }
    }

    try:
        resp = requests.post(f"{API_URL}/ingest", json=ingest_payload, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Ingest status {resp.status_code}: {resp.text}")
        data = resp.json()
        run_id = data.get("workflow_id") or data.get("run_id")
        if not run_id:
            raise RuntimeError(f"No run_id / workflow_id in ingest response: {data}")
    except Exception as e:
        print(f"[{f}] Ingest failed: {e}")
        rec = {
            "archivo": f, "source_key": source_key,
            "run_id": None, "estado": "failed_ingest",
            "caracteres": total_chars, "chunks": None,
            "estimado_usd": None, "estimado_alto_usd": None, "facturado_usd": None,
            "conceptos": None, "claims": None, "claims_verificados": None,
            "saltado": None, "error": str(e)
        }
        append_jsonl(rec)
        return "failed"

    print(f"[{f}] Started run_id: {run_id}")

    # Wait for gate (up to 30 minutes: 600 attempts * 3s)
    gate_url = f"{API_URL}/runs/{run_id}/gate"
    estimate_data = None
    for attempt in range(600):
        try:
            g_resp = requests.get(gate_url, timeout=10)
            if g_resp.status_code == 200:
                estimate_data = g_resp.json()
                break
            elif g_resp.status_code == 409:
                time.sleep(3)
            else:
                time.sleep(3)
        except Exception:
            time.sleep(3)

    if not estimate_data:
        print(f"[{f}] Gate timed out for {run_id} (30 mins exceeded). Rejecting run.")
        try:
            requests.post(f"{API_URL}/runs/{run_id}/approve", json={"approved": False, "options": ingest_payload["options"], "reason": "gate timeout 30m"}, timeout=10)
        except Exception:
            pass
        rec = {
            "archivo": f, "source_key": source_key,
            "run_id": run_id, "estado": "failed_gate_timeout",
            "caracteres": total_chars, "chunks": None,
            "estimado_usd": None, "estimado_alto_usd": None, "facturado_usd": None,
            "conceptos": None, "claims": None, "claims_verificados": None,
            "saltado": "Gate timeout", "error": "Gate timeout after 30 minutes"
        }
        append_jsonl(rec)
        return "failed"

    estimate = estimate_data.get("estimate", {})
    est_total = estimate.get("total_usd")
    est_high = estimate.get("total_usd_high")
    
    if est_total is None:
        print(f"[{f}] ERROR: Estimate total_usd is None (unpriced model). Rejecting run for safety.")
        try:
            requests.post(f"{API_URL}/runs/{run_id}/approve", json={"approved": False, "options": ingest_payload["options"], "reason": "unpriced model estimate is None"}, timeout=10)
        except Exception:
            pass
        rec = {
            "archivo": f, "source_key": source_key,
            "run_id": run_id, "estado": "rejected_unpriced",
            "caracteres": total_chars, "chunks": estimate_data.get("preview", {}).get("chunk_count"),
            "estimado_usd": None, "estimado_alto_usd": est_high, "facturado_usd": None,
            "conceptos": None, "claims": None, "claims_verificados": None,
            "saltado": "Estimate total_usd is None (unpriced model)", "error": None
        }
        append_jsonl(rec)
        return "failed"

    est_total_val = float(est_total)
    est_high_val = float(est_high) if est_high is not None else 0.0
    chunks_count = estimate_data.get("preview", {}).get("chunk_count")

    print(f"[{f}] Estimate: ${est_total_val:.4f} (High: ${est_high_val:.4f}) for {total_chars} chars, {chunks_count} chunks")

    max_allowed = max(total_chars * 0.0000067 * 3.0, 0.05)
    if est_total_val > max_allowed:
        print(f"[{f}] Sanity check FAILED: estimate ${est_total_val:.4f} > max allowed ${max_allowed:.4f}")
        requests.post(f"{API_URL}/runs/{run_id}/approve", json={"approved": False, "options": ingest_payload["options"], "reason": f"Sanity check failed: estimate {est_total_val} exceeds threshold {max_allowed}"}, timeout=10)
        rec = {
            "archivo": f, "source_key": source_key,
            "run_id": run_id, "estado": "rejected_sanity",
            "caracteres": total_chars, "chunks": chunks_count,
            "estimado_usd": est_total_val, "estimado_alto_usd": est_high_val, "facturado_usd": None,
            "conceptos": None, "claims": None, "claims_verificados": None,
            "saltado": f"Estimate ${est_total_val:.4f} exceeded sanity threshold ${max_allowed:.4f}", "error": None
        }
        append_jsonl(rec)
        return "skipped"

    app_resp = requests.post(f"{API_URL}/runs/{run_id}/approve", json={
        "approved": True,
        "options": ingest_payload["options"],
        "reason": "indexacion por lotes"
    }, timeout=30)

    if app_resp.status_code != 200:
        print(f"[{f}] Approval failed: {app_resp.text}")
        rec = {
            "archivo": f, "source_key": source_key,
            "run_id": run_id, "estado": "failed_approval",
            "caracteres": total_chars, "chunks": chunks_count,
            "estimado_usd": est_total_val, "estimado_alto_usd": est_high_val, "facturado_usd": get_run_cost(run_id),
            "conceptos": None, "claims": None, "claims_verificados": None,
            "saltado": None, "error": f"Approval failed: {app_resp.text}"
        }
        append_jsonl(rec)
        return "failed"

    print(f"[{f}] Approved. Waiting for completion (up to 35 minutes)...")

    state = "running"
    terminal_reached = False
    for _ in range(420):
        time.sleep(5)
        state = get_run_state(run_id)
        if state in ("succeeded", "failed", "cancelled"):
            terminal_reached = True
            break

    if not terminal_reached:
        print(f"[{f}] ERROR: Run {run_id} timed out waiting for terminal state.")
        rec = {
            "archivo": f, "source_key": source_key,
            "run_id": run_id, "estado": "timeout_terminal",
            "caracteres": total_chars, "chunks": chunks_count,
            "estimado_usd": est_total_val, "estimado_alto_usd": est_high_val, "facturado_usd": get_run_cost(run_id),
            "conceptos": None, "claims": None, "claims_verificados": None,
            "saltado": None, "error": "Timeout waiting for terminal state"
        }
        append_jsonl(rec)
        return "failed"

    print(f"[{f}] Run terminal state: {state}")

    conceptos = None
    claims = None
    claims_verificados = None
    try:
        r_details = requests.get(f"{API_URL}/runs/{run_id}", timeout=10)
        if r_details.status_code == 200:
            d_json = r_details.json()
            chunks_count = d_json.get("chunks_count", chunks_count) or chunks_count
            sem = d_json.get("semantics", {})
            if sem:
                conceptos = sem.get("concepts", 0)
                claims = sem.get("claims", 0)
                claims_verificados = sem.get("claims_verified", 0)
    except Exception as e:
        print(f"[{f}] Failed to fetch run details: {e}")

    facturado = get_run_cost(run_id)

    rec = {
        "archivo": f, "source_key": source_key,
        "run_id": run_id, "estado": state,
        "caracteres": total_chars, "chunks": chunks_count,
        "estimado_usd": est_total_val, "estimado_alto_usd": est_high_val, "facturado_usd": facturado,
        "conceptos": conceptos, "claims": claims, "claims_verificados": claims_verificados,
        "saltado": None, "error": None if state == "succeeded" else f"Run ended with state {state}"
    }
    append_jsonl(rec)
    return state

def main():
    initial_awaiting = get_awaiting_approval_count()
    print(f"Initial runs in awaiting_approval: {initial_awaiting}")

    if len(sys.argv) > 1:
        target = sys.argv[1]
        print(f"Processing single target: {target}")
        res = process_file(target)
        print(f"Result: {res}")
    else:
        pdfs = sorted([f for f in os.listdir(LIB_DIR) if f.endswith('.pdf')])
        for f in pdfs:
            res = process_file(f)
            if res not in ("succeeded", "skipped"):
                print(f"Stopping batch due to non-success result ({res}) on file {f}")
                break

    final_awaiting = get_awaiting_approval_count()
    print(f"\nFinal awaiting_approval: {final_awaiting}")

if __name__ == "__main__":
    main()
