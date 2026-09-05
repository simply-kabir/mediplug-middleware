"""
Normalize free-text mandatory-document strings into structured requirements.

Track B deliverable. This file works in two modes:

1. STANDALONE TEST MODE (run this file directly right now, no CSV needed):
   `python scripts/02_normalize_requirements.py --demo`
   Runs parse_requirement_string() against a handful of realistic sample
   strings so you can see exactly what it produces and sanity-check the
   logic before Track A's CSV even exists.

2. REAL PIPELINE MODE (once data/interim/packages_raw.csv exists):
   `python scripts/02_normalize_requirements.py`
   Reads the raw CSV, parses every row's document columns, and writes
   data/packages.json.
"""

import json
import re
import sys
from pathlib import Path

from rapidfuzz import fuzz, process

# --- Canonical document taxonomy -------------------------------------------
# Keys are canonical codes — these are what the pre-flight rule engine and
# the frontend will actually key off of. Extend this dict as you find new
# real strings in Track A's CSV (that's step 5 of the plan — iterate).
TAXONOMY: dict[str, dict] = {
    "usg":             {"label": "Ultrasound (USG)",   "aliases": ["usg", "ultrasound", "sonography", "u s"]},
    "usg_abdomen":     {"label": "USG Abdomen",        "aliases": ["usg abdomen", "usg abdominal"]},
    "ct_scan":         {"label": "CT Scan",            "aliases": ["ct", "ct scan", "cect", "ncct"]},
    "ct_angiogram":    {"label": "CT Angiogram",       "aliases": ["ct angio", "ct angiogram", "cta"]},
    "mri":             {"label": "MRI",                "aliases": ["mri", "mr imaging"]},
    "xray":            {"label": "X-Ray",              "aliases": ["x-ray", "xray", "x ray", "x- ray", "x rays", "skiagram"]},
    "echo_2d":         {"label": "2D Echo",            "aliases": ["2decho", "2d echo", "echo", "echocardiography"]},
    "angiogram":       {"label": "Angiogram",          "aliases": ["angio", "angiogram", "angiography"]},
    "doppler":         {"label": "Doppler",            "aliases": ["doppler", "colour doppler", "color doppler"]},
    "cbc":             {"label": "CBC Report",         "aliases": ["cbc", "complete blood count", "haemogram", "hemogram"]},
    "biopsy_report":   {"label": "Biopsy / Histopath", "aliases": [
        "biopsy", "histopath", "histopathology", "hpe",
        "histopatholigy", "histopathplogy"]},
    "scar_photo":      {"label": "Scar Photo",         "aliases": ["scar photo", "scar photograph", "post op photo"]},
    "endoscopy":       {"label": "Endoscopy Report",   "aliases": ["endoscopy", "ugi scopy", "upper gi endoscopy", "colonoscopy"]},
    "ecg":             {"label": "ECG",                "aliases": ["ecg", "ekg"]},
    "discharge_summary": {"label": "Discharge Summary", "aliases": ["discharge summary", "ds"]},
    "aadhaar_card":    {"label": "Aadhaar Card",       "aliases": ["aadhaar", "aadhar", "id proof"]},
    "ration_card":     {"label": "Ration Card",        "aliases": ["ration card"]},
    "ct_abdomen":      {"label": "CT Abdomen",         "aliases": ["ct abdomen", "cect abdomen", "ncct abdomen", "ct abd"]},
    "clinical_photograph": {"label": "Clinical Photograph", "aliases": [
        "clinical photograph", "clinical photo", "post procedure clinical photograph",
        "clinical photgraph", "clinical photgraph req", "clinical phoptograph",
        "endoscopic photograph"]},
    "clinical_notes":  {"label": "Clinical Notes",     "aliases": ["clinical notes"]},
    "operative_notes": {"label": "Operative/Operation Notes", "aliases": ["operative notes", "operation notes"]},
    "lab_investigations": {"label": "Lab Investigations", "aliases": ["lab investigations", "lab investigation"]},
    "radiological_investigations": {"label": "Radiological Investigations", "aliases": [
        "radiological investigations", "radiological investigation", "radiological evidence",
        "radiographic image", "radiographic images", "imagings", "imaging", "images"]},
    "chemo_sheets":    {"label": "Chemotherapy Sheets/Regimen", "aliases": [
        "chemo sheets", "chemotherapy regimen", "planned chemotherapy regimen at time of preauth",
        "chemo charts"]},
    "clinical_improvement_evidence": {"label": "Evidence of Clinical Improvement", "aliases": [
        "evidance of clinical imrovement", "evidence of clinical improvement",
        "post treatment evidence of clinical improvement", "post treatment evidence of clinical",
        "clinical improvement"]},
    "rft":             {"label": "RFT (Renal Function Test)", "aliases": ["rft"]},
    "lft":             {"label": "LFT (Liver Function Test)", "aliases": ["lft"]},
    "abg":             {"label": "ABG (Arterial Blood Gas)",  "aliases": ["abg"]},
    "sketch":          {"label": "Sketch",              "aliases": ["sketch"]},
    "ivp":             {"label": "IVP (Intravenous Pyelogram)", "aliases": ["ivp"]},
    "fnac":            {"label": "FNAC",                "aliases": ["fnac"]},
    "crp":             {"label": "CRP",                 "aliases": ["crp"]},
    "kub":             {"label": "KUB X-Ray",           "aliases": ["kub"]},
    "csf_analysis":    {"label": "CSF Analysis",        "aliases": ["csf analysis"]},
    "imaging_cd":      {"label": "Imaging CD",          "aliases": ["cd"]},
    "video_recording": {"label": "Video Recording",     "aliases": ["video"]},
    "pure_tone_audiogram": {"label": "Pure Tone Audiogram", "aliases": ["pure tone audiogram", "audiogram"]},
    "clinical_assessment": {"label": "Clinical Assessment", "aliases": ["clinical assessment"]},
    "rt_treatment_charts": {"label": "RT Treatment Charts", "aliases": ["rt treatment charts"]},
    "post_procedure_evidence": {"label": "Post Procedure Evidence", "aliases": [
        "post procedure evidence of surgery", "evidence of surgery",
        "post procedure evidance of surgery"]},
    "mcu":             {"label": "MCU (Micturating Cystourethrogram)", "aliases": ["mcu"]},
    "rgu":             {"label": "RGU (Retrograde Urethrogram)", "aliases": ["rgu"]},
    "emg":             {"label": "EMG",                 "aliases": ["emg"]},
    "fundus_photograph": {"label": "Fundus Photograph", "aliases": ["fundus photograph"]},
    "blood_culture":   {"label": "Blood Culture",       "aliases": ["blood culture"]},
    "b_scan":          {"label": "B-Scan",               "aliases": ["b scan"]},
    "serum_bilirubin": {"label": "Serum Bilirubin",     "aliases": ["serum bilirubin"]},
    "dsa":             {"label": "DSA (Digital Subtraction Angiography)", "aliases": ["dsa"]},
    "ot_notes":        {"label": "OT Notes",            "aliases": ["ot notes"]},
    "ear_microscopy":  {"label": "Ear Microscopy",      "aliases": ["ear microscopy"]},
    "eus":             {"label": "EUS (Endoscopic Ultrasound)", "aliases": ["eus"]},
    "biochemical_investigations": {"label": "Biochemical Investigations", "aliases": [
        "biochemical investigations", "biochemical investigation"]},

    # --- round 3: clearly-real doc/investigation types found in the CSV -----
    "psychologic_assessment": {"label": "Psychological Assessment", "aliases": [
        "psychologic assessment", "psychological assessment", "psychology assessment",
        "neuropsychological assessment"]},
    "ncs": {"label": "Nerve Conduction Study", "aliases": [
        "nerve conduction", "nerve conduction study", "nerve conduction studies", "ncs", "ncv"]},
    "ercp": {"label": "ERCP", "aliases": ["ercp"]},
    "mrcp": {"label": "MRCP", "aliases": ["mrcp"]},
    "ana": {"label": "ANA / Anti-dsDNA", "aliases": [
        "ana", "anti dsdna", "anti ds dna", "antinuclear antibody", "ana anti dsdna new or old report"]},
    "tumour_markers": {"label": "Tumour Markers", "aliases": [
        "tumour markers", "tumor markers", "tumour marker", "tumor marker"]},
    "cytology": {"label": "Cytology", "aliases": ["cytology", "cytology report"]},
    "bone_marrow": {"label": "Bone Marrow Study", "aliases": [
        "bone marrow", "bone marrow biopsy", "bone marrow aspiration", "bone marrow examination"]},
    "tft": {"label": "Thyroid Function Test", "aliases": [
        "tft", "t f t", "thyroid function test", "thyroid profile"]},
    "thyroid_scan": {"label": "Thyroid Scan", "aliases": ["thyroid scan", "thyroid scan optional"]},
    "skin_swab": {"label": "Skin Swab / Culture", "aliases": [
        "skin swab", "swab culture", "wound swab", "pus culture", "pus c s", "pus c"]},
    "blood_sugar": {"label": "Blood Sugar", "aliases": [
        "blood sugar", "blood glucose", "rbs", "fbs", "ppbs", "random blood sugar",
        "fasting blood sugar"]},
    "serum_electrolytes": {"label": "Serum Electrolytes", "aliases": [
        "electrolytes", "electolytes", "serum electrolytes", "sr electrolytes",
        "sr electrolites", "s electrolytes"]},
    "serum_amylase": {"label": "Serum Amylase", "aliases": ["sr amylase", "serum amylase", "s amylase"]},
    "serum_lipase": {"label": "Serum Lipase", "aliases": ["sr lipase", "serum lipase", "s lipase"]},
    "serum_creatinine": {"label": "Serum Creatinine", "aliases": [
        "sr creat", "sr creatinine", "serum creatinine", "s creatinine", "sr creatnine"]},
    "serum_protein": {"label": "Serum Protein", "aliases": [
        "s protein", "sr protein", "serum protein", "total protein"]},
    "coagulation_profile": {"label": "Coagulation Profile (PT/aPTT/INR)", "aliases": [
        "pt", "aptt", "pt inr", "inr", "coagulation profile", "s fibrinogen level",
        "fibrinogen", "prothrombin time"]},
    "bence_jones_protein": {"label": "Bence Jones Protein (Urine)", "aliases": [
        "bence jones protiens urine", "bence jones protein", "bence jones proteins",
        "bence jones protein urine"]},
    "serum_immunoelectrophoresis": {"label": "Serum Immunoelectrophoresis", "aliases": [
        "s immune electrophoresis", "serum immunoelectrophoresis",
        "serum protein electrophoresis", "spep", "immunoelectrophoresis"]},
    "bills": {"label": "Bills / Invoice Copy", "aliases": [
        "bills copy", "bills", "bill copy", "hospital bills", "invoice", "final bill"]},
    "urine_routine": {"label": "Urine Routine / Microscopy", "aliases": [
        "urine r m", "urine routine", "urine r", "urine routine microscopy", "urine analysis"]},

    # --- round 4: remaining clearly-real ENT / lab / imaging types ---------
    "chest_xray": {"label": "Chest X-Ray", "aliases": [
        "cxr", "chest x ray", "x ray chest", "xray chest", "chest radiograph"]},
    "hrct_chest": {"label": "HRCT Chest", "aliases": [
        "hrct chest", "hrct thorax", "hrct", "hrct lung"]},
    "eeg": {"label": "EEG", "aliases": ["eeg", "electroencephalogram", "video eeg"]},
    "opg": {"label": "OPG (Orthopantomogram)", "aliases": ["opg", "orthopantomogram", "opg xray"]},
    "renal_scan": {"label": "Renal Scan (DTPA/DMSA)", "aliases": [
        "renal scan", "dtpa scan", "dmsa scan", "renal dtpa", "renal dmsa"]},
    "beta_hcg": {"label": "Beta HCG", "aliases": ["b hcg", "beta hcg", "bhcg", "serum beta hcg"]},
    "serum_calcium": {"label": "Serum Calcium", "aliases": [
        "sr calcium level", "serum calcium", "sr calcium", "s calcium", "calcium level"]},
    "barium_study": {"label": "Barium Study", "aliases": [
        "barium", "barium study", "barium studies", "barium swallow", "barium meal", "barium enema"]},
    "cystoscopy": {"label": "Cystoscopy", "aliases": ["cystoscopy", "cystoscopy report"]},
    "platelet_count": {"label": "Platelet Count", "aliases": ["platelet count", "platelets"]},
    "fluid_analysis": {"label": "Fluid Analysis", "aliases": [
        "fluid analysis", "pleural fluid analysis", "ascitic fluid analysis", "body fluid analysis"]},
    "culture_sensitivity": {"label": "Culture & Sensitivity", "aliases": [
        "culture sensitivity", "c s", "culture and sensitivity", "sensitivity"]},
    # ENT endoscopic exam findings — indirect laryngoscopy / diagnostic nasal endoscopy
    "ent_endoscopy_findings": {"label": "ENT Endoscopy Findings (IDL/DNE)", "aliases": [
        "idl findings", "idl examination finding", "idl photograph", "idl",
        "dne", "dne findings", "diagnostic nasal endoscopy",
        "indirect laryngoscopy", "ear findings", "ear findings by ent surgeon",
        "ear findings by ent", "nasal endoscopy"]},
    "diagram_burns": {"label": "Burns Area Diagram (Rule of 9)", "aliases": [
        "diagramatic distribution of burns mandatory rule of 9",
        "diagramatic distribution of burns", "rule of 9", "rule of nine",
        "burns distribution diagram", "diagrammatic distribution of burns"]},
}

# Photos of the operated site show up under many phrasings — all satisfy the
# same "photo of the surgical site" requirement.
TAXONOMY["clinical_photograph"]["aliases"] += [
    "post surgery photo", "post procedure photo", "post op photograph",
    "post operative photograph", "preop photograph",
    "clinical photo of affected part", "photo of affected part",
    "clinical photo showing suture", "operative photograph", "intra op photograph",
]

# --- round 5: long tail from the real 1670-row CSV -------------------------
# Everything here was read off the actual unmapped dump, not guessed. Grouped
# by what the document IS, so the rule engine can key off one code per real
# artefact rather than one per phrasing.
TAXONOMY.update({
    # -- labs: haematology ---------------------------------------------------
    "peripheral_smear": {"label": "Peripheral Smear", "aliases": [
        "peripheral smear", "peripheral blood smear", "pbs report", "pbs", "ps", "p s"]},
    "esr": {"label": "ESR", "aliases": ["esr", "erythrocyte sedimentation rate"]},
    "reticulocyte_count": {"label": "Reticulocyte Count", "aliases": [
        "reticulocyte count", "reticulocytes count", "retic count"]},
    "hb_electrophoresis": {"label": "Hb Electrophoresis", "aliases": [
        "hb electrophoresis", "haemoglobin electrophoresis", "hemoglobin electrophoresis",
        "electrophoresis", "electrophorosis", "electrophorosis and other confirmatory test",
        "hplc"]},
    "iron_studies": {"label": "Iron Studies / B12 / Ferritin", "aliases": [
        "serum ferritin", "sr ferritin", "ferritin", "serum ferritin 6 monthly req",
        "sr fe", "serum iron", "b12", "vit b12 level", "vitamin b12", "vit b12"]},
    "coombs_test": {"label": "Coombs Test", "aliases": [
        "coombs test", "coomb s test", "coombs", "direct coombs test"]},

    # -- labs: biochemistry --------------------------------------------------
    "biochemistry_panel": {"label": "Biochemistry Panel", "aliases": [
        "albumin", "serum albumin", "phosphorus", "serum phosphorus", "pth",
        "alkaline phosphatase", "serum alkaline phosphatase", "sr alkaline phosphatase",
        "reports of serum chemistry", "serum chemistry"]},
    "blood_urea": {"label": "Blood Urea / BUN", "aliases": [
        "blood urea", "bun", "uric acid", "sr uric acid", "serum uric acid"]},
    "serum_ammonia": {"label": "Serum Ammonia", "aliases": [
        "s ammonia", "sr ammonia", "serum ammonia", "sr ammonia optional", "ammonia"]},
    "cholinesterase": {"label": "Serum Cholinesterase", "aliases": [
        "sr cholinesterase", "serum cholinesterase", "cholinesterase"]},
    "cardiac_enzymes": {"label": "Cardiac Enzymes", "aliases": [
        "cardiac enzymes", "cardiac enzyme", "troponin", "trop t", "cpkmb", "cpk mb", "cpk"]},
    "ada": {"label": "ADA (Adenosine Deaminase)", "aliases": ["ada", "adenosine deaminase"]},

    # -- labs: endocrine -----------------------------------------------------
    "hormone_assay": {"label": "Hormone Assay", "aliases": [
        "lh", "fsh", "prolactin", "testosterone", "testerterone", "testerterone males",
        "estriol", "dheas", "igf1", "igf 1", "acth assay", "basal acth",
        "gh stimulation test", "post glucose gh assay", "cortisol assay",
        "cortisol assay after dexamethasone", "basal cortisol", "serum cortisol",
        "serum cortizol", "basal cortisol post acth cortisol", "basal cortisol cost fsh",
        "hormone assays", "hormone assay", "endocrine evaluation",
        "water deprivation test", "water deprivation test if needed",
        "serum metarphines", "serum hormetamerphines", "metanephrines"]},
    "karyotyping": {"label": "Karyotyping", "aliases": ["karyotyping", "karyotype"]},

    # -- labs: immunology / serology ----------------------------------------
    "autoimmune_panel": {"label": "Autoimmune Panel", "aliases": [
        "dsdna", "anca", "c3", "c4", "c4 optional", "c3 c4",
        "c3 marker to be given to confirm the dignosis of septic shock",
        "anti scl 70", "anti u1 rnp ab", "anti u1 rnp", "acl antibodies",
        "ra", "ra factor", "rheumatoid factor"]},
    "serology": {"label": "Serology", "aliases": [
        "dengue serology", "serology", "hbsag", "hepatic viral studies hepatitis b",
        "hepatitis c", "hepatitis a optional", "hepatic viral studies",
        "cryptococcal antigen", "investigation foe cryptococcal antigen",
        "test for detection of malarial parasite", "malarial parasite",
        "test for p falciparum parasite", "rapid test"]},
    "flow_cytometry": {"label": "Flow Cytometry / Surface Markers", "aliases": [
        "cytochemistry", "surface markers", "flow cytometry", "immunophenotyping"]},
    "serum_afp": {"label": "Serum AFP", "aliases": [
        "serum afp", "s a fp", "afp", "alpha fetoprotein"]},

    # -- micro --------------------------------------------------------------
    "urine_culture": {"label": "Urine Culture", "aliases": ["urine culture", "urine c"]},
    "sputum_studies": {"label": "Sputum / Washing Studies", "aliases": [
        "sputum", "sputum culture", "sputum gram stain", "gram stain",
        "bronchial washing", "aspirate for culture"]},
    "fungal_microscopy": {"label": "Direct Microscopy (Fungal Hyphae)", "aliases": [
        "tzanck smear", "direct microscopy", "fungal hyphae",
        "direct microscopy report to confirm fungal hyphae is mandatory during preauth",
        "direct microscopy report for fungal hyphae is mandatory during preauth",
        "koh mount"]},
    "drug_susceptibility": {"label": "Drug Susceptibility Test", "aliases": [
        "drug susceptibility", "dst", "drug sensitivity"]},

    # -- imaging -------------------------------------------------------------
    "bone_scan": {"label": "Bone Scan / Skeletal Survey", "aliases": [
        "bone scan", "skeletal study", "skeletal survey"]},
    "pet_scan": {"label": "PET Scan", "aliases": ["pet", "pet ct", "pet scan", "psma"]},
    "mammography": {"label": "Mammography", "aliases": [
        "mammography", "mamography", "mammogram"]},
    "contrast_study": {"label": "Contrast Study", "aliases": [
        "contrast study", "contrast", "t tube cholangiogram", "cholangiogram",
        "distal cologram", "cologram", "hsg", "sialography", "silography",
        "dcg", "dcg dacryocystogram", "dacryocystogram", "contrast ugi",
        "per cutaneous transhepatic billiary", "per cutaneous transhepatic"]},
    "mr_angiography": {"label": "MR Angiography / Venography", "aliases": [
        "mra", "mrv", "mr angiography", "mr venography"]},
    "neurosonogram": {"label": "Neurosonogram", "aliases": [
        "neurosonogram", "cranial ultrasound"]},
    "dexa_scan": {"label": "DEXA Scan", "aliases": [
        "dexa", "dexa scan", "dexa of hip spine", "bmd", "bone densitometry"]},

    # -- functional / physiological studies ----------------------------------
    "pft": {"label": "Pulmonary Function Test", "aliases": [
        "pft", "spirometry", "pulmonary function test", "pulmonary function test major"]},
    "manometry": {"label": "Manometry", "aliases": ["manometry", "oesophageal manometry"]},
    "polysomnography": {"label": "Polysomnography", "aliases": [
        "polysomnography", "sleep study"]},
    "evoked_potentials": {"label": "Evoked Potentials / RNS", "aliases": [
        "veps", "vep", "visual evoked potential", "rns", "repetitive nerve stimulation",
        "neostigmine test", "neostgmine test"]},
    "cvp_monitoring": {"label": "CVP Monitoring", "aliases": [
        "cvp monitoring", "cvp monitoring optional", "cvp"]},

    # -- ophthalmology -------------------------------------------------------
    "oct": {"label": "OCT (Optical Coherence Tomography)", "aliases": [
        "oct", "optical coherence tomography"]},
    "iop_evidence": {"label": "Intraocular Pressure / Disc Findings", "aliases": [
        "iop", "evidence of raised iop", "intraocular pressure", "optic disc changes",
        "grading of retinopathy by opthalmologist", "grading of retinopathy"]},

    # -- scores, records, admin ----------------------------------------------
    "severity_score": {"label": "Severity Score (Ranson/BISAP/APACHE/APGAR)", "aliases": [
        "ranson", "ranson scoring", "bisap scoring", "bisap score at time of admission",
        "bisap score", "apache score", "apache score at time of admission",
        "apgar score", "apgar score mandatory"]},
    "mlc": {"label": "MLC Papers", "aliases": ["mlc", "medico legal case", "mlc papers"]},
    "diet_chart": {"label": "Diet Chart", "aliases": ["diet chart"]},
    "transfusion_record": {"label": "Transfusion / Therapy Record", "aliases": [
        "record of blood transfusion", "blood transfusion record",
        "evidence of platelet transfusion", "evidence of platelet transfusion if given",
        "case sheet with records of factor given",
        "case sheet with records of chelation therapy"]},
    "justification_letter": {"label": "Treating Doctor's Justification", "aliases": [
        "justification", "certificate", "proof of treatment",
        "patient has to be certified by cardiologist",
        "evidence of treatment resulting in refractory cardiac failure"]},
    "implant_sticker": {"label": "Implant / Stent Sticker", "aliases": [
        "sticker", "stickers", "barcode sticker", "bar code sticker", "bar code stickers",
        "implant sticker", "implant sticker barcode", "stent sticker", "stent stickers",
        "iol sticker", "iabp sticker",
        "chemotherapy drug batch number with bar code",
        "carton of the stents used approved by fda"]},
    "drug_stock_evidence": {"label": "Drug Stock Photograph", "aliases": [
        "photograph of patient given stock of six months injection",
        "stock of six months injection", "tablets"]},
    "serum_dsg1": {"label": "Serum DSG-1", "aliases": [
        "serum dsg 1", "dsg 1", "desmoglein"]},
})

# Alias top-ups on codes that already exist — same document, more phrasings
# seen in the real data.
TAXONOMY["endoscopy"]["aliases"] += [
    "scopy", "gastroscopy", "broncoscopy", "bronchoscopy", "ureteroscopy", "uds",
    "endoscopic image", "endoscopic picture", "scopy photo",
    "endoscopic drainage", "endoscopic billiary drainage",
]
TAXONOMY["csf_analysis"]["aliases"] += [
    "csf", "cell count", "csf india ink preparation", "csf antibodies for hsv",
]
TAXONOMY["coagulation_profile"]["aliases"] += [
    "thromboplastin time", "pttk", "bt", "dic profile",
    "factor viii", "ix assay coagulation parameters", "von will brands", "von willebrand",
]
TAXONOMY["serum_electrolytes"]["aliases"] += [
    "serum sodium", "serum potassium", "sr pottassium", "sr potassium",
    "electrolyte studies",
]
TAXONOMY["serum_creatinine"]["aliases"] += ["creatinine", "creatinine level"]
TAXONOMY["serum_calcium"]["aliases"] += ["calcium"]
TAXONOMY["serum_amylase"]["aliases"] += ["amylase"]
TAXONOMY["serum_lipase"]["aliases"] += ["lipase"]
TAXONOMY["serum_bilirubin"]["aliases"] += ["sr bilirubin", "bilirubin"]
TAXONOMY["cbc"]["aliases"] += ["haematocrit", "hematocrit", "blood test"]
TAXONOMY["tft"]["aliases"] += ["t3 t4 tsh", "t4 tsh", "t3", "t4", "tsh"]
TAXONOMY["thyroid_scan"]["aliases"] += ["iodine scan"]
TAXONOMY["renal_scan"]["aliases"] += ["renogram"]
TAXONOMY["pure_tone_audiogram"]["aliases"] += [
    "pta", "pta findings", "audiometry", "impedance audiometry", "machine generated audiometry",
]
TAXONOMY["urine_routine"]["aliases"] += [
    "urine albumin", "24 hour urine protein", "urine ketone", "urine exam",
    "routine urine at the time of preauth", "urine bs bp",
]
TAXONOMY["biopsy_report"]["aliases"] += [
    "hpr", "hp report", "hp report at the time of claims", "hp req at the time of claim",
    "pathology", "pathology if primary", "tumour biospy", "tumour biopsy",
]
TAXONOMY["operative_notes"]["aliases"] += [
    "procedure notes", "proceedure notes", "case sheet",
    "proceedure notes sufficient for peripheral arterial embolectomy",
]
TAXONOMY["clinical_notes"]["aliases"] += [
    "clinical findings", "clinical finding", "clinical finiding", "clinical history",
    "clinical history of acid ingestion", "clinical indications",
    "risk assessment and investigations", "investigations justyfying the diagnosis",
    "investigation indicative of disease", "etiology of pancreatitis",
]
TAXONOMY["clinical_photograph"]["aliases"] += [
    "patient photograph", "post operative patient photograph after 6 weeks",
    "post op specimen photo", "specimen photo", "during procedure photo",
    "clinical picture", "intra op photo showing mesh",
    "photograph",
]
TAXONOMY["scar_photo"]["aliases"] += [
    "scar", "incision", "suture", "suture req at the time of claim", "suture line photo",
]
TAXONOMY["angiogram"]["aliases"] += [
    "anigo", "cag", "cag stills", "cag stills showing blocks", "aortogram", "venogram",
    "fluoroscopic image", "angiographic image", "post procedure angiographic image",
]
TAXONOMY["radiological_investigations"]["aliases"] += [
    "inmaging", "imaging to show evidence of urinary obstruction",
    "inmaging to show evidence of urinary obstruction",
]
TAXONOMY["post_procedure_evidence"]["aliases"] += [
    "evidence of blockage", "evidence of laryngectomy", "evidence of use of prosthesis",
    "evidence of anterior nasal packing and post nasal packing",
    "evidence of stopage of epistaxis", "evidence of stent placement",
    "post treatment evidence of photograph of stent in position",
]
TAXONOMY["fundus_photograph"]["aliases"] += [
    "fundus photo", "post procedure evidence of fundus photo",
]
TAXONOMY["rt_treatment_charts"]["aliases"] += ["data of rt treatment plan", "rt treatment plan"]
TAXONOMY["bills"]["aliases"] += ["bill", "bill of spinal brace"]
TAXONOMY["lab_investigations"]["aliases"] += ["clinical investigations"]
TAXONOMY["tumour_markers"]["aliases"] += ["psa", "ca 125", "ca125", "cea", "cea optional"]
TAXONOMY["beta_hcg"]["aliases"] += ["b hc"]
TAXONOMY["rgu"]["aliases"] += ["retrograde urethrogram"]
TAXONOMY["rft"]["aliases"] += ["kft", "kidney function test"]
TAXONOMY["biopsy_report"]["aliases"] += ["hp"]
# "BT CT" is bleeding time / clotting time, NOT a CT scan — without this the
# "ct" alias grabs it.
TAXONOMY["coagulation_profile"]["aliases"] += ["bt ct", "bleeding time", "clotting time"]


_ALIAS_TO_CODE = {
    alias: code
    for code, meta in TAXONOMY.items()
    for alias in meta["aliases"]
}

# --- Delimiter semantics ----------------------------------------------------
# "/"  → OR   (alternatives — any ONE of these satisfies the requirement)
# ","  → AND  (all required)   ← verify against real data, it's the shakier rule
# "&"  → AND
# "+"  → AND
# NOTE: the word "and" is deliberately NOT a splitter. In the real data it
# almost always sits *inside* a single doc name ("Clinical and radiological
# investigations", "notes and sketch"), so splitting on it just produced
# orphan fragments like "Clinical". "&" still splits.
# Medical shorthand that uses "/" as part of the term, not as an OR. These
# must be expanded BEFORE splitting, otherwise "Blood C/S" shatters into the
# orphan fragments "Blood C" and "S" — which is exactly where a chunk of the
# unmapped tail was coming from.
SLASH_SHORTHAND: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bc\s*/\s*s\b", re.IGNORECASE), "culture sensitivity"),
    (re.compile(r"\bc\s*/\s*o\b", re.IGNORECASE), "complaints of"),
    (re.compile(r"\br\s*/\s*o\b", re.IGNORECASE), "rule out"),
    (re.compile(r"\bh\s*/\s*o\b", re.IGNORECASE), "history of"),
    (re.compile(r"\bs\s*/\s*o\b", re.IGNORECASE), "suggestive of"),
    (re.compile(r"\bw\s*/\s*o\b", re.IGNORECASE), "without"),
    (re.compile(r"\be\s*/\s*o\b", re.IGNORECASE), "evidence of"),
    (re.compile(r"\bi\s*/\s*v\b", re.IGNORECASE), "intravenous"),
    (re.compile(r"\bp\s*/\s*s\b", re.IGNORECASE), "peripheral smear"),
    (re.compile(r"\bs\.?\s*a\s*/\s*fp\b", re.IGNORECASE), "serum afp"),
    (re.compile(r"\bb\s*/\s*hcg?\b", re.IGNORECASE), "beta hcg"),
    (re.compile(r"\bu\s*/\s*s\b", re.IGNORECASE), "ultrasound"),
]


def repair_shorthand(raw: str) -> str:
    for pattern, replacement in SLASH_SHORTHAND:
        raw = pattern.sub(replacement, raw)
    return raw


AND_SPLIT = re.compile(r"\s*(?:,|&|\+)\s*")
OR_SPLIT = re.compile(r"\s*(?:/|\bor\b)\s*", re.IGNORECASE)

# --- Fuzzy typo fallback --------------------------------------------------
# Only spelled-out aliases (>=5 chars) are fuzzy targets — matching a typo
# against a 2-3 char abbreviation ("cta", "abg") is where false positives
# live. fuzz.ratio (pure edit distance) + a high cutoff keeps this to real
# misspellings: "sckech"->"sketch" (83), "protien"->"protein", while
# truncated fragments ("Notes", "Incision", "Post Surgery Photo") stay <78.
FUZZY_ALIASES = [a for a in {  # dict-from-comprehension keeps it unique
    alias: 1 for meta in TAXONOMY.values() for alias in meta["aliases"] if len(alias) >= 5
}]
FUZZY_CUTOFF = 82
FUZZY_MIN_LEN = 5

# Fragments that are not documents at all: leftovers from splitting a
# parenthetical or a slash, and bare qualifiers. Dropping these is honest —
# reporting "3" or "old report)" as an unmapped *requirement* is noise that
# hides the real gaps.
DROP_FRAGMENTS = {
    "name", "name of patient preferably printed", "dose", "stills",
    "lat", "lateral", "pelvis", "nodes", "thorax and abdomen",
    "new", "old", "old report", "new report", "when necessary",
    "brain pns chest when necessary", "48 hrs post admission",
    "physician for the same", "source of infection", "dcgi only",
    "alkali", "corrosive", "acidosis", "esp of no evidence of alkalosis",
    "invasive ventilator should be discretion of treating physician",
    "bipap is not accepted",
    "all legal formalities of transplantation of human organs act shall be completed",
    "for ards packages ventilator with niv mask", "cpap",
}

NOISE = re.compile(r"\b(report|reports|film|films|copy|if applicable|wherever applicable|where applicable|as applicable)\b", re.IGNORECASE)


def _is_truncation(key: str, alias: str) -> bool:
    """True when `key` is just a shorter form of `alias`, not a misspelling of it.

    fuzz.ratio scores "calcium" vs "s calcium" at 87 — high enough to pass the
    cutoff, but it is not a typo, it is a *less specific* phrase. Accepting it
    silently pins a generic term to a body-part-specific code ("microscopy" ->
    Ear Microscopy, "photograph" -> IDL Photograph). If every word of the input
    already appears in the alias and the alias adds more, it is a truncation.
    """
    key_words, alias_words = set(key.split()), set(alias.split())
    return key_words < alias_words


def canonicalize(fragment: str) -> dict | None:
    """Map one free-text fragment to a canonical doc code.
    Returns None for fragments that are pure boilerplate/noise (e.g. "where
    applicable" with nothing else) — these aren't a document at all, so they
    get dropped rather than flagged unmapped.
    """
    key = NOISE.sub("", fragment).strip().lower()
    key = re.sub(r"[^a-z0-9]+", " ", key)   # punctuation (-, ., etc.) -> space
    key = re.sub(r"\s+", " ", key).strip()

    if not key:
        return None

    if key in _ALIAS_TO_CODE:
        code = _ALIAS_TO_CODE[key]
        return {"code": code, "label": TAXONOMY[code]["label"]}

    # Word-boundary fallback — longest alias wins (so "ct angio" beats "ct").
    # \b boundaries mean short abbreviations like "abg" match as their own
    # word ("machine generated abg") without matching inside unrelated
    # longer words.
    best = None
    for alias, code in _ALIAS_TO_CODE.items():
        pattern = r"\b" + re.escape(alias) + r"\b"
        if re.search(pattern, key) and (best is None or len(alias) > len(best[0])):
            best = (alias, code)
    if best:
        code = best[1]
        return {"code": code, "label": TAXONOMY[code]["label"]}

    if key in DROP_FRAGMENTS or len(key) <= 2:
        return None

    # Fuzzy fallback — catch spelling typos without hand-listing every variant.
    if len(key) >= FUZZY_MIN_LEN:
        hit = process.extractOne(
            key, FUZZY_ALIASES, scorer=fuzz.ratio, score_cutoff=FUZZY_CUTOFF
        )
        if hit and not _is_truncation(key, hit[0]):
            alias = hit[0]
            code = _ALIAS_TO_CODE[alias]
            return {
                "code": code,
                "label": TAXONOMY[code]["label"],
                "fuzzy": {"from": key, "alias": alias, "score": round(hit[1], 1)},
            }

    # Unknown — keep it, flag it for review instead of silently dropping it
    slug = re.sub(r"[^a-z0-9]+", "_", key).strip("_") or "unknown"
    return {"code": f"unmapped__{slug}", "label": fragment.strip(), "unmapped": True}


def parse_requirement_string(raw: str, stage: str) -> list[dict]:
    """'Angiogram /Doppler/ CT angio, CBC' →
    [ {any_of:[angio,doppler,cta]}, {any_of:[cbc]} ]

    The AND split happens first (splits into separate requirements),
    THEN the OR split happens within each piece (alternatives inside
    one requirement).
    """
    if not raw or raw.strip().lower() in {"", "-", "na", "n/a", "nil", "none"}:
        return []

    requirements = []
    for i, and_part in enumerate(AND_SPLIT.split(repair_shorthand(raw))):
        if not and_part.strip():
            continue
        options = [
            o for o in (canonicalize(p) for p in OR_SPLIT.split(and_part) if p.strip())
            if o is not None
        ]
        seen_codes = set()
        deduped = []
        for o in options:
            if o["code"] not in seen_codes:
                seen_codes.add(o["code"])
                deduped.append(o)
        options = deduped
        if not options:
            continue
        requirements.append({
            "requirement_id": f"req_{i}",
            "stage": stage,
            "any_of": options,
            "human_label": " or ".join(o["label"] for o in options),
        })
    return requirements


# ---------------------------------------------------------------------------
# Standalone demo — run this today, no CSV needed
# ---------------------------------------------------------------------------

DEMO_SAMPLES = [
    "Angiogram /Doppler/ CT angio, CBC",
    "USG Abdomen or CT Abdomen",
    "2D Echo",
    "X-Ray & ECG",
    "Biopsy report / Histopathology",
    "Discharge Summary, Aadhaar Card, Ration Card",
    "-",
    "N/A",
    "Some Totally Unknown Document Type",
]


def demo() -> None:
    print("=== Track B standalone test — no CSV needed ===\n")
    for raw in DEMO_SAMPLES:
        print(f"Input:  {raw!r}")
        parsed = parse_requirement_string(raw, stage="preauth")
        if not parsed:
            print("  → (no requirements)")
        for req in parsed:
            flags = [o["code"] for o in req["any_of"] if o.get("unmapped")]
            unmapped_note = f"  ⚠ UNMAPPED: {flags}" if flags else ""
            print(f"  → {req['human_label']}{unmapped_note}")
        print()


# ---------------------------------------------------------------------------
# Real pipeline mode — needs Track A's data/interim/packages_raw.csv
# ---------------------------------------------------------------------------


def main() -> None:
    raw_path = Path("data/interim/packages_raw.csv")
    out_path = Path("data/packages.json")

    if not raw_path.exists():
        sys.exit(
            f"Missing {raw_path} — that's Track A's output.\n"
            f"Run with --demo instead to test the parser standalone:\n"
            f"  python scripts/02_normalize_requirements.py --demo"
        )

    import csv

    packages = []
    unmapped_counter: dict[str, int] = {}

    with raw_path.open(encoding="utf-8") as f:
        for row in csv.reader(f):
            # ADJUST THESE INDICES after Track A shares the real CSV shape
            try:
                code, spec, subspec, name, amount, pre_docs, claim_docs, status, icd = row[:9]
            except ValueError:
                continue
            if not re.match(r"^S\d", code.strip()):  # skip headers/junk
                continue

            pre = parse_requirement_string(pre_docs, "preauth")
            clm = parse_requirement_string(claim_docs, "claim")

            for r in pre + clm:
                for o in r["any_of"]:
                    if o.get("unmapped"):
                        unmapped_counter[o["label"]] = unmapped_counter.get(o["label"], 0) + 1

            packages.append({
                "code": code.strip(),
                "speciality": spec.strip(),
                "sub_speciality": subspec.strip(),
                "name": re.sub(r"\s+", " ", name).strip(),
                "amount": int(re.sub(r"[^\d]", "", amount) or 0),
                "icd_code": icd.strip(),
                "status": status.strip(),
                "requirements": {"preauth": pre, "claim": clm},
                "search_text": f"{name} {spec} {subspec}".strip(),
            })

    out_path.write_text(json.dumps(packages, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(packages)} packages → {out_path}")

    print("\nTop 30 unmapped document strings — add these to TAXONOMY:")
    for label, n in sorted(unmapped_counter.items(), key=lambda x: -x[1])[:30]:
        print(f"  {n:>4}  {label}")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        main()