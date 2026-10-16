#!/usr/bin/env python3
"""
codifica_temi.py
================
Codifica qualitativa delle risposte aperte di un form e raggruppamento in macro-temi.

Flusso:
  1. Legge il file Excel/CSV esportato da Google Fogli (File > Scarica > Microsoft Excel).
  2. CODEBOOK: usa il tuo codebook (se lo passi con --codebook) oppure ne genera uno
     automaticamente leggendo le risposte (codifica induttiva).
  3. CODIFICA: assegna a ogni risposta da 1 a 3 codici del codebook.
  4. RAGGRUPPAMENTO: conta e raccoglie le risposte che condividono lo stesso codice.
  5. TEMI: aggrega i codici in macro-temi.
  6. Scrive un Excel di output con 4 fogli:
       - Risposte codificate  (risposta originale + codici + tema)
       - Codebook             (codice, etichetta, descrizione, n. risposte, tema)
       - Risposte per codice  (le risposte raggruppate sotto ogni codice)
       - Temi                 (macro-tema, descrizione, codici, n. risposte, citazioni)

Requisiti:
  pip install pandas openpyxl
  A) In locale con Ollama (gratis, i dati non escono dal PC):
       installa Ollama da https://ollama.com, poi:  ollama pull qwen2.5:14b
       e aggiungi  --ollama  al comando.
  B) Con l'API Claude:
       pip install anthropic
       export ANTHROPIC_API_KEY="la-tua-chiave"   (Windows: set ANTHROPIC_API_KEY=...)

Esempi:
  # In locale con Ollama
  python codifica_temi.py risposte.xlsx --colonna "Cosa miglioreresti?" --ollama
  python codifica_temi.py risposte.xlsx --colonna 3 --ollama --modello gemma3:12b

  # Codici generati automaticamente, domanda nella colonna "Cosa miglioreresti?"
  python codifica_temi.py risposte.xlsx --colonna "Cosa miglioreresti?"

  # Con un tuo codebook (Excel/CSV con colonne: codice, descrizione)
  python codifica_temi.py risposte.xlsx --colonna "Cosa miglioreresti?" --codebook codici.xlsx

  # Elenca le colonne del file per scegliere quella giusta
  python codifica_temi.py risposte.xlsx --elenca-colonne
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

MODELLO_DEFAULT = "claude-sonnet-5-5"
MODELLO_OLLAMA_DEFAULT = "qwen2.5:14b"
SPECIALI = {
    "ALTRO": "Contenuto pertinente non coperto da nessun altro codice",
    "NP": "Risposta non pertinente o vuota di contenuto (es. 'no', 'niente', '-')",
}

SISTEMA = (
    "Sei un ricercatore esperto di analisi qualitativa (analisi tematica, Braun & Clarke). "
    "Lavori su risposte aperte a questionari, in italiano. Sei rigoroso, fedele al testo, "
    "non inventi contenuti. Rispondi SEMPRE e SOLO con un oggetto JSON valido, senza testo prima o dopo."
)


# --------------------------------------------------------------------------- utilità

def leggi_tabella(percorso: str, foglio=None) -> pd.DataFrame:
    p = Path(percorso)
    if p.suffix.lower() == ".csv":
        return pd.read_csv(p)
    return pd.read_excel(p, sheet_name=foglio or 0)


def a_blocchi(lista, n):
    for i in range(0, len(lista), n):
        yield lista[i:i + n]


class ClienteAnthropic:
    """Modelli Claude via API (serve ANTHROPIC_API_KEY)."""

    def __init__(self):
        from anthropic import Anthropic
        self.client = Anthropic()

    def completa(self, modello, prompt, max_tokens):
        r = self.client.messages.create(
            model=modello,
            max_tokens=max_tokens,
            system=SISTEMA,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


class ClienteOllama:
    """Modelli locali via Ollama (https://ollama.com). Nessuna chiave, i dati restano sul PC."""

    def __init__(self, url="http://localhost:11434", contesto=16384, timeout=900):
        import urllib.request
        self.urllib = urllib.request
        self.url = url.rstrip("/")
        self.contesto = contesto
        self.timeout = timeout
        try:  # controllo che Ollama sia acceso
            self.urllib.urlopen(f"{self.url}/api/tags", timeout=5).read()
        except Exception:
            sys.exit(f"Ollama non risponde su {self.url}. Avvialo (app Ollama o 'ollama serve') e riprova.")

    def completa(self, modello, prompt, max_tokens):
        corpo = json.dumps({
            "model": modello,
            "stream": False,
            "format": "json",  # obbliga il modello a rispondere in JSON
            "messages": [{"role": "system", "content": SISTEMA},
                         {"role": "user", "content": prompt}],
            "options": {"temperature": 0.2, "num_ctx": self.contesto, "num_predict": max_tokens},
        }).encode("utf-8")
        req = self.urllib.Request(f"{self.url}/api/chat", data=corpo,
                                  headers={"Content-Type": "application/json"})
        with self.urllib.urlopen(req, timeout=self.timeout) as r:
            dati = json.loads(r.read().decode("utf-8"))
        if "error" in dati:
            raise RuntimeError(dati["error"])
        return dati["message"]["content"]


def chiedi_json(client, modello, prompt, max_tokens=8000, tentativi=4):
    """Chiama il modello e restituisce il JSON della risposta (con nuovi tentativi)."""
    ultimo_errore = None
    for t in range(tentativi):
        try:
            testo = client.completa(modello, prompt, max_tokens)
            inizio, fine = testo.find("{"), testo.rfind("}")
            if inizio == -1 or fine == -1:
                raise ValueError("nessun JSON nella risposta")
            return json.loads(testo[inizio:fine + 1])
        except Exception as e:  # JSON malformato, rate limit, errori di rete...
            ultimo_errore = e
            attesa = 5 * (t + 1)
            print(f"   ! tentativo {t + 1} fallito ({e}); riprovo tra {attesa}s", file=sys.stderr)
            time.sleep(attesa)
    raise RuntimeError(f"Chiamata al modello fallita dopo {tentativi} tentativi: {ultimo_errore}")


def formatta_risposte(righe):
    return "\n".join(f"[{r['id']}] {r['testo']}" for r in righe)


def formatta_codebook(codebook):
    return "\n".join(f"- {c['codice']} | {c['etichetta']}: {c['descrizione']}" for c in codebook)


# --------------------------------------------------------------------------- 1. codebook

def carica_codebook(percorso):
    df = leggi_tabella(percorso)
    df.columns = [str(c).strip().lower() for c in df.columns]
    col_cod = "codice" if "codice" in df.columns else df.columns[0]
    col_desc = "descrizione" if "descrizione" in df.columns else df.columns[-1]
    col_etich = "etichetta" if "etichetta" in df.columns else col_cod
    codebook = []
    for _, r in df.dropna(subset=[col_cod]).iterrows():
        codebook.append({
            "codice": str(r[col_cod]).strip(),
            "etichetta": str(r[col_etich]).strip(),
            "descrizione": "" if pd.isna(r[col_desc]) else str(r[col_desc]).strip(),
        })
    return codebook


def genera_codebook(client, modello, righe, domanda, max_codici, dim_blocco=150):
    """Codifica induttiva: propone codici per blocchi di risposte, poi li unifica."""
    proposte = []
    blocchi = list(a_blocchi(righe, dim_blocco))
    for i, blocco in enumerate(blocchi, 1):
        print(f"   codebook: lettura blocco {i}/{len(blocchi)}")
        prompt = f"""Domanda del questionario: «{domanda}»

Leggi queste risposte e proponi i CODICI qualitativi che descrivono i contenuti ricorrenti.
Ogni codice deve essere specifico (un'idea sola), descrittivo e radicato nel testo.

RISPOSTE:
{formatta_risposte(blocco)}

Restituisci JSON:
{{"codici": [{{"etichetta": "nome breve (2-5 parole)", "descrizione": "cosa include, in una frase"}}]}}"""
        proposte += chiedi_json(client, modello, prompt)["codici"]

    print("   codebook: unificazione dei codici proposti")
    elenco = "\n".join(f"- {c['etichetta']}: {c['descrizione']}" for c in proposte)
    prompt = f"""Domanda del questionario: «{domanda}»

Questi codici sono stati proposti leggendo blocchi diversi di risposte. Unisci i duplicati e i
quasi-sinonimi, mantieni distinti i concetti diversi, e produci un codebook finale di al massimo
{max_codici} codici. Numera i codici C01, C02, ...

CODICI PROPOSTI:
{elenco}

Restituisci JSON:
{{"codici": [{{"codice": "C01", "etichetta": "...", "descrizione": "..."}}]}}"""
    return chiedi_json(client, modello, prompt)["codici"]


# --------------------------------------------------------------------------- 2. codifica

def codifica_risposte(client, modello, righe, domanda, codebook, dim_blocco):
    validi = {c["codice"] for c in codebook} | set(SPECIALI)
    speciali = "\n".join(f"- {k}: {v}" for k, v in SPECIALI.items())
    risultato = {}
    blocchi = list(a_blocchi(righe, dim_blocco))
    for i, blocco in enumerate(blocchi, 1):
        print(f"   codifica: blocco {i}/{len(blocchi)}")
        prompt = f"""Domanda del questionario: «{domanda}»

CODEBOOK:
{formatta_codebook(codebook)}
Codici speciali:
{speciali}

Assegna a ciascuna risposta da 1 a 3 codici del codebook (solo quelli davvero presenti nel testo,
dal più rilevante al meno rilevante). Usa ALTRO o NP solo quando nessun codice si applica.

RISPOSTE:
{formatta_risposte(blocco)}

Restituisci JSON con TUTTE le risposte, usando gli id tra parentesi quadre:
{{"codifiche": [{{"id": 1, "codici": ["C03", "C07"]}}]}}"""
        dati = chiedi_json(client, modello, prompt)
        for c in dati.get("codifiche", []):
            codici = [x for x in c.get("codici", []) if x in validi]
            risultato[int(c["id"])] = codici or ["ALTRO"]
        mancanti = [r["id"] for r in blocco if r["id"] not in risultato]
        if mancanti:
            print(f"   ! {len(mancanti)} risposte non codificate nel blocco {i}, segnate ALTRO", file=sys.stderr)
            for m in mancanti:
                risultato[m] = ["ALTRO"]
    return risultato


# --------------------------------------------------------------------------- 3. temi

def genera_temi(client, modello, domanda, codebook, per_codice, max_temi):
    righe = []
    for c in codebook:
        esempi = per_codice.get(c["codice"], [])[:3]
        es = " / ".join(f"«{t[:150]}»" for t in esempi)
        righe.append(f"- {c['codice']} | {c['etichetta']} ({len(per_codice.get(c['codice'], []))} risposte): "
                     f"{c['descrizione']}  Esempi: {es}")
    prompt = f"""Domanda del questionario: «{domanda}»

Ecco i codici emersi dalla codifica, con frequenza ed esempi:
{chr(10).join(righe)}

Raggruppa i codici in MACRO-TEMI (da 3 a {max_temi}). Un tema è un'idea più ampia che dà senso a
più codici, non una semplice categoria. Ogni codice va in UN SOLO tema. Ignora i codici con 0 risposte
se non si adattano a nessun tema (mettili comunque nel tema più vicino).

Restituisci JSON:
{{"temi": [{{"tema": "nome del tema", "descrizione": "2-3 frasi che spiegano il tema", "codici": ["C01", "C04"]}}]}}"""
    return chiedi_json(client, modello, prompt)["temi"]


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Codifica risposte aperte di un form e le raggruppa in temi.")
    ap.add_argument("file", help="Excel (.xlsx) o CSV con le risposte del form")
    ap.add_argument("--colonna", help="Nome (o numero, da 1) della colonna con le risposte da analizzare")
    ap.add_argument("--foglio", help="Nome del foglio Excel (default: il primo)")
    ap.add_argument("--codebook", help="Excel/CSV con il tuo codebook (colonne: codice, [etichetta], descrizione)")
    ap.add_argument("--domanda", help="Testo della domanda (default: intestazione della colonna)")
    ap.add_argument("--output", help="File Excel di output (default: <file>_temi.xlsx)")
    ap.add_argument("--max-codici", type=int, default=30, help="Max codici se generati in automatico (default 30)")
    ap.add_argument("--max-temi", type=int, default=7, help="Max macro-temi (default 7)")
    ap.add_argument("--ollama", action="store_true",
                    help="Usa un modello locale con Ollama invece dell'API Claude")
    ap.add_argument("--modello", help=f"Modello (default: {MODELLO_DEFAULT}; con --ollama: {MODELLO_OLLAMA_DEFAULT})")
    ap.add_argument("--ollama-url", default="http://localhost:11434", help="Indirizzo di Ollama")
    ap.add_argument("--contesto", type=int, default=16384,
                    help="Finestra di contesto per Ollama in token (default 16384; alzala se hai RAM/VRAM)")
    ap.add_argument("--blocco", type=int,
                    help="Risposte per chiamata in fase di codifica (default 40; con --ollama 15)")
    ap.add_argument("--blocco-codebook", type=int,
                    help="Risposte per chiamata nella generazione del codebook (default 150; con --ollama 60)")
    ap.add_argument("--elenca-colonne", action="store_true", help="Mostra le colonne del file ed esce")
    args = ap.parse_args()

    df = leggi_tabella(args.file, args.foglio)

    if args.elenca_colonne or not args.colonna:
        print("Colonne disponibili:")
        for i, c in enumerate(df.columns, 1):
            print(f"  {i:>2}. {c}")
        if not args.colonna:
            print("\nIndica quella da analizzare con --colonna \"nome\" (o --colonna numero).")
        return

    colonna = args.colonna
    if colonna not in df.columns and colonna.isdigit():
        colonna = df.columns[int(colonna) - 1]
    if colonna not in df.columns:
        sys.exit(f"Colonna «{args.colonna}» non trovata. Usa --elenca-colonne per vederle.")
    domanda = args.domanda or str(colonna)

    # righe valide (non vuote); id = numero di riga nel foglio (intestazione = riga 1)
    righe = []
    for idx, valore in df[colonna].items():
        if pd.notna(valore) and str(valore).strip():
            righe.append({"id": int(idx) + 2, "testo": " ".join(str(valore).split())})
    if not righe:
        sys.exit("La colonna non contiene risposte.")
    print(f"→ {len(righe)} risposte da analizzare nella colonna «{colonna}»")

    if args.ollama:
        client = ClienteOllama(args.ollama_url, args.contesto)
        args.modello = args.modello or MODELLO_OLLAMA_DEFAULT
        args.blocco = args.blocco or 15
        args.blocco_codebook = args.blocco_codebook or 60
        print(f"→ Modello locale Ollama: {args.modello}")
    else:
        client = ClienteAnthropic()  # legge ANTHROPIC_API_KEY dall'ambiente
        args.modello = args.modello or MODELLO_DEFAULT
        args.blocco = args.blocco or 40
        args.blocco_codebook = args.blocco_codebook or 150

    # 1. codebook
    if args.codebook:
        codebook = carica_codebook(args.codebook)
        print(f"→ Codebook caricato: {len(codebook)} codici")
    else:
        print("→ Generazione automatica del codebook")
        codebook = genera_codebook(client, args.modello, righe, domanda, args.max_codici,
                                   args.blocco_codebook)
        print(f"→ Codebook generato: {len(codebook)} codici")
    tutti_codici = codebook + [{"codice": k, "etichetta": k, "descrizione": v} for k, v in SPECIALI.items()]
    etichette = {c["codice"]: c["etichetta"] for c in tutti_codici}

    # 2. codifica
    print("→ Codifica delle risposte")
    codifiche = codifica_risposte(client, args.modello, righe, domanda, codebook, args.blocco)

    # 3. raggruppamento per codice
    per_codice = defaultdict(list)
    righe_per_codice = defaultdict(list)
    for r in righe:
        for c in codifiche[r["id"]]:
            per_codice[c].append(r["testo"])
            righe_per_codice[c].append(r)

    # 4. temi
    print("→ Raggruppamento dei codici in macro-temi")
    temi = genera_temi(client, args.modello, domanda, codebook, per_codice, args.max_temi)
    tema_di = {}
    for t in temi:
        for c in t["codici"]:
            tema_di.setdefault(c, t["tema"])
    for k in SPECIALI:
        tema_di.setdefault(k, "(non tematizzato)")

    # 5. output
    righe_out = []
    for r in righe:
        codici = codifiche[r["id"]]
        temi_r = list(dict.fromkeys(tema_di.get(c, "(non assegnato)") for c in codici))
        righe_out.append({
            "Riga": r["id"],
            "Risposta": r["testo"],
            "Codici": ", ".join(codici),
            "Etichette": "; ".join(etichette.get(c, c) for c in codici),
            "Tema principale": temi_r[0],
            "Tutti i temi": "; ".join(temi_r),
        })
    df_risposte = pd.DataFrame(righe_out)

    df_codebook = pd.DataFrame([{
        "Codice": c["codice"], "Etichetta": c["etichetta"], "Descrizione": c["descrizione"],
        "N. risposte": len(per_codice.get(c["codice"], [])),
        "% risposte": round(100 * len(per_codice.get(c["codice"], [])) / len(righe), 1),
        "Tema": tema_di.get(c["codice"], "(non assegnato)"),
    } for c in tutti_codici]).sort_values(["Tema", "N. risposte"], ascending=[True, False])

    gruppi = []
    for c in df_codebook["Codice"]:
        for r in righe_per_codice.get(c, []):
            gruppi.append({"Tema": tema_di.get(c, ""), "Codice": c,
                           "Etichetta": etichette.get(c, c), "Riga": r["id"], "Risposta": r["testo"]})
    df_gruppi = pd.DataFrame(gruppi)

    temi_out = []
    for t in temi:
        ids = {r["id"] for c in t["codici"] for r in righe_per_codice.get(c, [])}
        citazioni = list(dict.fromkeys(
            r["testo"] for c in t["codici"] for r in righe_per_codice.get(c, [])))[:3]
        temi_out.append({
            "Tema": t["tema"], "Descrizione": t["descrizione"],
            "Codici": ", ".join(f"{c} ({etichette.get(c, c)})" for c in t["codici"]),
            "N. risposte": len(ids), "% risposte": round(100 * len(ids) / len(righe), 1),
            "Citazioni di esempio": "\n".join(f"«{q}»" for q in citazioni),
        })
    df_temi = pd.DataFrame(temi_out).sort_values("N. risposte", ascending=False)

    output = args.output or str(Path(args.file).with_name(Path(args.file).stem + "_temi.xlsx"))
    with pd.ExcelWriter(output, engine="openpyxl") as w:
        for nome, d in [("Temi", df_temi), ("Codebook", df_codebook),
                        ("Risposte per codice", df_gruppi), ("Risposte codificate", df_risposte)]:
            d.to_excel(w, sheet_name=nome, index=False)
            foglio = w.sheets[nome]
            foglio.freeze_panes = "A2"
            for col in foglio.columns:
                larghezza = max(len(str(cella.value or "")) for cella in col[:200])
                foglio.column_dimensions[col[0].column_letter].width = min(max(10, larghezza + 2), 80)

    print(f"\n✓ Fatto. Risultati in: {output}")
    for _, t in df_temi.iterrows():
        print(f"   • {t['Tema']} — {t['N. risposte']} risposte ({t['% risposte']}%)")


if __name__ == "__main__":
    main()