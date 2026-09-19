"use client";

import { useState, useEffect, useRef } from "react";
import { createClient } from "@supabase/supabase-js";
import { 
  Search, AlertCircle, Clock, CheckCircle2, UploadCloud, 
  Activity, X, ChevronRight, ShieldCheck, Loader2,
  FileText, Check, Hospital, UserCheck, Stethoscope, RefreshCw,
  Upload, Trash2, ExternalLink, Paperclip, ShieldAlert, Ban,
  Plus, ListPlus, Send
} from "lucide-react";

// ---------------------------------------------------------------------------
// Supabase Client Initialization (Direct Realtime Connection)
// ---------------------------------------------------------------------------
// The hardcoded fallback KEY here used to be a service_role JWT — RLS-bypassing,
// full-privilege, shipped to every visitor, and in git history. It is gone.
// Supply the ANON key via env (rls.sql grants public SELECT on all four tables,
// so Realtime works on anon and no write path opens up). Copy
// frontend/.env.example -> frontend/.env.local (gitignored) and fill it in.
const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL || "https://rnmjetheinheufqqsfeq.supabase.co";
const SUPABASE_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "";
const GATEWAY_URL = process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:8000";
const ADMIN_TOKEN = process.env.NEXT_PUBLIC_ADMIN_TOKEN || "dev-admin-token";

if (typeof window !== "undefined" && !SUPABASE_KEY) {
  // Loud runtime failure (not a build-time throw — client components still
  // prerender on the server). The UI will show "Disconnected".
  console.error(
    "Missing NEXT_PUBLIC_SUPABASE_ANON_KEY — copy frontend/.env.example to frontend/.env.local and set the anon key."
  );
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY || "anon-key-not-set");

type Case = {
  id: string;
  tracking_ref: string;
  hms_case_ref: string;
  stage: string;
  status:
    | 'queued'
    | 'analyzing'
    | 'needs_code_confirmation'
    | 'action_required'
    | 'ready_for_dispatch'
    | 'dispatching'
    | 'submitted'
    | 'dispatch_failed'
    | 'payer_approved'
    | 'payer_rejected';
  patient: { name: string; gender: string; birth_date: string };
  encounter: { attending_doctor: string; hospital_id: string };
  raw_clinical_notes: string;
  missing_requirements?: {
    requirement_id?: string;
    human_label: string;
    satisfied: boolean;
    violation_type?: string;
    any_of?: (string | { code: string; label?: string })[];
  }[];
  mapped_package_code?: string;
  mapped_package_name?: string;
  confidence?: number;
  alternate_codes?: { code: string; name: string; confidence: number }[];
  payer_correlation_id?: string;
  error_message?: string;
};

type CaseDocument = {
  id: string;
  document_type: string;
  file_url: string;
  file_name?: string;
  uploaded_at?: string;
};

type QueueStats = {
  stream_length: number;
  dlq_length: number;
  pending: number;
};

export default function AarogyamitraPortal() {
  const [cases, setCases] = useState<Case[]>([]);
  const [selectedCase, setSelectedCase] = useState<Case | null>(null);
  const [selectedPackageCode, setSelectedPackageCode] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("ALL");
  const [queueStats, setQueueStats] = useState<QueueStats | null>(null);

  // Document upload & attachments state
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [selectedDocType, setSelectedDocType] = useState<string>("usg_abdomen");
  const [uploading, setUploading] = useState(false);
  const [uploadSuccess, setUploadSuccess] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [caseDocuments, setCaseDocuments] = useState<CaseDocument[]>([]);
  const [loadingDocs, setLoadingDocs] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Batch upload state (Feature 1)
  const [stagedFiles, setStagedFiles] = useState<{ file: File; docType: string }[]>([]);

  // Manual dispatch state (Feature 2)
  const [dispatching, setDispatching] = useState(false);
  const [dispatchSuccess, setDispatchSuccess] = useState<string | null>(null);
  const [dispatchError, setDispatchError] = useState<string | null>(null);

  // Live queue-depth panel — guide §11.2 ("a strong architecture demo"),
  // stretch_goals.md item 2. Polls the gateway's /admin/queue every 2s.
  useEffect(() => {
    let active = true;
    async function pollQueue() {
      try {
        const res = await fetch(`${GATEWAY_URL}/admin/queue`, {
          headers: { "X-Admin-Token": ADMIN_TOKEN },
        });
        if (!res.ok) throw new Error(`admin/queue ${res.status}`);
        const data = await res.json();
        if (active) setQueueStats(data);
      } catch {
        if (active) setQueueStats(null);
      }
    }
    pollQueue();
    const id = setInterval(pollQueue, 2000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    // 1. Initial Load: Fetch all live cases directly from Supabase
    async function loadInitialCases() {
      try {
        setLoading(true);
        const { data, error: dbError } = await supabase
          .from("cases")
          .select("*")
          .order("created_at", { ascending: false });

        if (dbError) throw dbError;
        setCases((data as Case[]) || []);
        setError(null);
      } catch (err: any) {
        console.error("Error loading cases from Supabase:", err);
        setError("Failed to connect to Supabase Realtime DB.");
      } finally {
        setLoading(false);
      }
    }

    loadInitialCases();

    // 2. Realtime WebSocket: Subscribe to INSERT and UPDATE on cases table
    const channel = supabase
      .channel("realtime-cases")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "cases" },
        (payload) => {
          console.log("Realtime event received from MediPlug:", payload);

          if (payload.eventType === "INSERT") {
            const newCase = payload.new as Case;
            setCases((prev) => [newCase, ...prev]);
          } else if (payload.eventType === "UPDATE") {
            const updatedCase = payload.new as Case;
            setCases((prev) =>
              prev.map((c) => (c.id === updatedCase.id ? updatedCase : c))
            );
            // If the modal is open for this case, update its live state inside the modal too
            setSelectedCase((prev) =>
              prev && prev.id === updatedCase.id ? updatedCase : prev
            );
          }
        }
      )
      .subscribe((status) => {
        if (status === "SUBSCRIBED") {
          console.log("Connected to Supabase Realtime!");
        }
      });

    return () => {
      supabase.removeChannel(channel);
    };
  }, []);

  // When modal opens, pre-select the highest confidence candidate
  useEffect(() => {
    if (selectedCase?.alternate_codes && selectedCase.alternate_codes.length > 0) {
      setSelectedPackageCode(selectedCase.alternate_codes[0].code);
    } else {
      setSelectedPackageCode(null);
    }
  }, [selectedCase]);

  // Load existing documents and pre-select document type when a case is selected
  useEffect(() => {
    if (!selectedCase) {
      setCaseDocuments([]);
      setUploadFile(null);
      setStagedFiles([]);
      setUploadSuccess(null);
      setUploadError(null);
      setDispatchSuccess(null);
      setDispatchError(null);
      return;
    }

    // Default the doc type to the first legitimate missing document requirement (excluding fraud alerts)
    const legitDocReqs = (selectedCase.missing_requirements || []).filter(
      (req) => !req.violation_type && !(req.human_label && req.human_label.startsWith("FRAUD ALERT"))
    );

    if (legitDocReqs.length > 0) {
      const firstReq = legitDocReqs[0];
      let code = "";
      if (firstReq.any_of && firstReq.any_of.length > 0) {
        const opt = firstReq.any_of[0];
        code = typeof opt === "string" ? opt : opt.code;
      }
      setSelectedDocType(code || "usg_abdomen");
    } else {
      setSelectedDocType("usg_abdomen");
    }

    // Fetch existing documents for this case
    async function fetchDocs() {
      if (!selectedCase) return;
      try {
        setLoadingDocs(true);
        const res = await fetch(`${GATEWAY_URL}/api/v1/cases/${selectedCase.id}/documents`);
        if (res.ok) {
          const data = await res.json();
          setCaseDocuments(data);
        }
      } catch (err) {
        console.error("Failed to load case documents:", err);
      } finally {
        setLoadingDocs(false);
      }
    }
    fetchDocs();
  }, [selectedCase?.id]);

  // 3. Confirm Code: Hits our gateway POST /confirm-code endpoint
  const handleConfirmCode = async () => {
    if (!selectedCase || !selectedPackageCode) return;
    try {
      setConfirming(true);
      const res = await fetch(`${GATEWAY_URL}/api/v1/cases/${selectedCase.id}/confirm-code`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          code: selectedPackageCode,
          confirmed_by: "aarogyamitra-ui",
        }),
      });

      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail || "Failed to confirm package code");
      }
    } catch (err: any) {
      console.error("Error confirming code:", err);
      alert(err.message || "Failed to confirm package code.");
    } finally {
      setConfirming(false);
    }
  };

  // 4. Upload Document: Hits our gateway POST /upload-document endpoint
  const handleFileSelect = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const file = files[0];
    setUploadFile(file);
    setUploadError(null);
    setUploadSuccess(null);
  };

  const handleUploadDocument = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!selectedCase || !uploadFile) {
      setUploadError("Please select a file to upload.");
      return;
    }
    if (!selectedDocType.trim()) {
      setUploadError("Please choose a document type.");
      return;
    }

    try {
      setUploading(true);
      setUploadError(null);
      setUploadSuccess(null);

      const formData = new FormData();
      formData.append("file", uploadFile);
      formData.append("document_type", selectedDocType.trim());
      formData.append("uploaded_by", "aarogyamitra-ui");

      const res = await fetch(`${GATEWAY_URL}/api/v1/cases/${selectedCase.id}/upload-document`, {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to upload document");
      }

      setUploadSuccess(`"${uploadFile.name}" uploaded successfully! Pre-flight rules are re-evaluating...`);
      setUploadFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";

      // Refresh documents list
      const docsRes = await fetch(`${GATEWAY_URL}/api/v1/cases/${selectedCase.id}/documents`);
      if (docsRes.ok) {
        const updatedDocs = await docsRes.json();
        setCaseDocuments(updatedDocs);
      }
    } catch (err: any) {
      console.error("Upload error:", err);
      setUploadError(err.message || "Failed to upload file.");
    } finally {
      setUploading(false);
    }
  };

  // 5. Batch Document Upload (Feature 1): Queue multiple docs and upload all at once
  const handleStageFile = () => {
    if (!uploadFile) {
      setUploadError("Please select a file to stage for batch upload.");
      return;
    }
    setStagedFiles((prev) => [...prev, { file: uploadFile, docType: selectedDocType }]);
    setUploadFile(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
    setUploadError(null);
    setUploadSuccess(`Staged "${uploadFile.name}" as [${selectedDocType}].`);
  };

  const handleRemoveStaged = (idx: number) => {
    setStagedFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleBatchUpload = async () => {
    if (!selectedCase) return;
    if (stagedFiles.length === 0) {
      setUploadError("No documents staged for batch upload.");
      return;
    }

    try {
      setUploading(true);
      setUploadError(null);
      setUploadSuccess(null);

      const formData = new FormData();
      stagedFiles.forEach((item) => {
        formData.append("files", item.file);
        formData.append("document_types", item.docType);
      });
      formData.append("uploaded_by", "aarogyamitra-ui");

      const res = await fetch(`${GATEWAY_URL}/api/v1/cases/${selectedCase.id}/upload-documents-batch`, {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to upload documents in batch");
      }

      const count = stagedFiles.length;
      setStagedFiles([]);
      setUploadSuccess(`Successfully uploaded ${count} document(s) in batch! Pre-flight rules are evaluating.`);

      // Refresh documents list
      const docsRes = await fetch(`${GATEWAY_URL}/api/v1/cases/${selectedCase.id}/documents`);
      if (docsRes.ok) {
        const updatedDocs = await docsRes.json();
        setCaseDocuments(updatedDocs);
      }
    } catch (err: any) {
      console.error("Batch upload error:", err);
      setUploadError(err.message || "Failed to upload documents in batch.");
    } finally {
      setUploading(false);
    }
  };

  // 6. Manual Dispatch (Feature 2): Dispatch directly if automatic dispatch failed or worker restarted
  const handleManualDispatch = async () => {
    if (!selectedCase) return;
    try {
      setDispatching(true);
      setDispatchError(null);
      setDispatchSuccess(null);

      const res = await fetch(`${GATEWAY_URL}/api/v1/cases/${selectedCase.id}/manual-dispatch`, {
        method: "POST",
      });

      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || "Manual dispatch failed");
      }

      setDispatchSuccess("Manual dispatch triggered! Worker will evaluate pre-flight rules and submit claim to NHCX.");
    } catch (err: any) {
      console.error("Manual dispatch error:", err);
      setDispatchError(err.message || "Failed to trigger manual dispatch.");
    } finally {
      setDispatching(false);
    }
  };

  const renderStatusBadge = (status: string) => {
    switch (status) {
      case 'analyzing': 
        return <span className="inline-flex items-center px-2.5 py-1 bg-amber-50 text-amber-700 border border-amber-200 rounded-full text-xs font-semibold tracking-wide"><Clock className="w-3.5 h-3.5 mr-1 animate-spin text-amber-500" /> Analyzing</span>;
      case 'action_required': 
        return <span className="inline-flex items-center px-2.5 py-1 bg-rose-50 text-rose-700 border border-rose-200 rounded-full text-xs font-semibold tracking-wide"><AlertCircle className="w-3.5 h-3.5 mr-1 text-rose-500" /> Action Required</span>;
      case 'needs_code_confirmation': 
        return <span className="inline-flex items-center px-2.5 py-1 bg-indigo-50 text-indigo-700 border border-indigo-200 rounded-full text-xs font-semibold tracking-wide shadow-sm"><Activity className="w-3.5 h-3.5 mr-1 text-indigo-500" /> Confirm Code</span>;
      case 'ready_for_dispatch':
        return <span className="inline-flex items-center px-2.5 py-1 bg-emerald-50 text-emerald-700 border border-emerald-200 rounded-full text-xs font-semibold tracking-wide"><CheckCircle2 className="w-3.5 h-3.5 mr-1 text-emerald-500" /> Dispatch Ready</span>;
      case 'dispatching':
        return <span className="inline-flex items-center px-2.5 py-1 bg-sky-50 text-sky-700 border border-sky-200 rounded-full text-xs font-semibold tracking-wide"><Loader2 className="w-3.5 h-3.5 mr-1 animate-spin text-sky-500" /> Dispatching</span>;
      case 'submitted':
        return <span className="inline-flex items-center px-2.5 py-1 bg-blue-50 text-blue-700 border border-blue-200 rounded-full text-xs font-semibold tracking-wide"><UploadCloud className="w-3.5 h-3.5 mr-1 text-blue-500" /> Submitted to Payer</span>;
      case 'dispatch_failed':
        return <span className="inline-flex items-center px-2.5 py-1 bg-rose-50 text-rose-700 border border-rose-200 rounded-full text-xs font-semibold tracking-wide"><AlertCircle className="w-3.5 h-3.5 mr-1 text-rose-500" /> Dispatch Failed</span>;
      case 'payer_approved':
        return <span className="inline-flex items-center px-2.5 py-1 bg-emerald-600 text-white border border-emerald-700 rounded-full text-xs font-bold tracking-wide shadow-sm"><CheckCircle2 className="w-3.5 h-3.5 mr-1 text-white" /> Payer Approved</span>;
      case 'payer_rejected':
        return <span className="inline-flex items-center px-2.5 py-1 bg-rose-600 text-white border border-rose-700 rounded-full text-xs font-bold tracking-wide shadow-sm"><X className="w-3.5 h-3.5 mr-1 text-white" /> Payer Rejected</span>;
      case 'queued':
        return <span className="inline-flex items-center px-2.5 py-1 bg-slate-100 text-slate-600 border border-slate-200 rounded-full text-xs font-medium">Queued</span>;
      default:
        // Never lie about the state — render whatever the row actually says.
        return <span className="inline-flex items-center px-2.5 py-1 bg-slate-100 text-slate-600 border border-slate-200 rounded-full text-xs font-medium">{status}</span>;
    }
  };

  const stats = {
    total: cases.length,
    needsConfirm: cases.filter(c => c.status === 'needs_code_confirmation').length,
    actionReq: cases.filter(c => c.status === 'action_required').length,
    ready: cases.filter(c => c.status === 'ready_for_dispatch').length,
    submitted: cases.filter(c => c.status === 'submitted' || c.status === 'dispatching').length,
    approved: cases.filter(c => c.status === 'payer_approved').length,
  };

  const filteredCases = cases.filter((c) => {
    const matchesSearch = 
      (c.patient?.name || "").toLowerCase().includes(searchQuery.toLowerCase()) ||
      (c.tracking_ref || "").toLowerCase().includes(searchQuery.toLowerCase()) ||
      (c.hms_case_ref || "").toLowerCase().includes(searchQuery.toLowerCase());
    
    if (statusFilter === "ALL") return matchesSearch;
    return matchesSearch && c.status === statusFilter;
  });

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 antialiased font-sans">
      {/* Top Navbar */}
      <header className="bg-slate-900 text-white sticky top-0 z-40 border-b border-slate-800 shadow-md">
        <div className="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="w-9 h-9 bg-indigo-600 rounded-xl flex items-center justify-center shadow-lg shadow-indigo-500/30">
              <ShieldCheck className="w-5 h-5 text-white" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h1 className="text-lg font-bold tracking-tight text-white">MediPlug</h1>
                <span className="bg-indigo-500/20 text-indigo-300 text-[10px] font-semibold px-2 py-0.5 rounded-full border border-indigo-500/30">MJPJAY PreAuth</span>
              </div>
              <p className="text-[11px] text-slate-400">Aarogyamitra Operational Console</p>
            </div>
          </div>

          <div className="flex items-center space-x-3">
            {queueStats && (
              <div className="hidden sm:flex items-center space-x-3 px-3 py-1.5 bg-slate-800/80 rounded-full border border-slate-700/80 backdrop-blur-sm font-mono text-xs text-slate-300">
                <span title="Redis stream length">queue <span className="font-bold text-white">{queueStats.stream_length}</span></span>
                <span className="text-slate-600">|</span>
                <span title="Unacked (in-flight) messages">pending <span className="font-bold text-white">{queueStats.pending}</span></span>
                <span className="text-slate-600">|</span>
                <span title="Dead-letter queue length" className={queueStats.dlq_length > 0 ? "text-rose-400" : ""}>
                  dlq <span className="font-bold">{queueStats.dlq_length}</span>
                </span>
              </div>
            )}
            <div className="flex items-center px-3 py-1.5 bg-slate-800/80 rounded-full border border-slate-700/80 backdrop-blur-sm">
              <div className={`w-2 h-2 rounded-full mr-2 ${error ? 'bg-rose-500' : 'bg-emerald-400 animate-pulse'}`}></div>
              <span className="text-xs font-medium text-slate-300">
                {error ? 'Disconnected' : 'Supabase Realtime 🟢'}
              </span>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-8">
        {/* Metric Cards Banner */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
          <div 
            onClick={() => setStatusFilter("ALL")}
            className={`p-5 rounded-2xl border transition-all cursor-pointer ${
              statusFilter === "ALL" 
                ? "bg-white border-indigo-500 shadow-md ring-2 ring-indigo-500/20" 
                : "bg-white border-slate-200 shadow-sm hover:border-slate-300"
            }`}
          >
            <div className="text-xs font-bold text-slate-500 uppercase tracking-wider">Total Active Cases</div>
            <div className="text-3xl font-extrabold text-slate-900 mt-2">{stats.total}</div>
            <div className="text-xs text-slate-400 mt-1">Live from PostgreSQL</div>
          </div>

          <div 
            onClick={() => setStatusFilter("needs_code_confirmation")}
            className={`p-5 rounded-2xl border transition-all cursor-pointer ${
              statusFilter === "needs_code_confirmation" 
                ? "bg-white border-indigo-600 shadow-md ring-2 ring-indigo-500/20" 
                : "bg-white border-slate-200 shadow-sm hover:border-indigo-300"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold text-indigo-600 uppercase tracking-wider">Awaiting Confirmation</span>
              <Activity className="w-4 h-4 text-indigo-500" />
            </div>
            <div className="text-3xl font-extrabold text-indigo-600 mt-2">{stats.needsConfirm}</div>
            <div className="text-xs text-indigo-400 mt-1">AI Package Matches</div>
          </div>

          <div 
            onClick={() => setStatusFilter("action_required")}
            className={`p-5 rounded-2xl border transition-all cursor-pointer ${
              statusFilter === "action_required" 
                ? "bg-white border-rose-500 shadow-md ring-2 ring-rose-500/20" 
                : "bg-white border-slate-200 shadow-sm hover:border-rose-300"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold text-rose-600 uppercase tracking-wider">Action Required</span>
              <AlertCircle className="w-4 h-4 text-rose-500" />
            </div>
            <div className="text-3xl font-extrabold text-rose-600 mt-2">{stats.actionReq}</div>
            <div className="text-xs text-rose-400 mt-1">Missing Documents / Low Conf</div>
          </div>

          <div 
            onClick={() => setStatusFilter("ready_for_dispatch")}
            className={`p-5 rounded-2xl border transition-all cursor-pointer ${
              statusFilter === "ready_for_dispatch" 
                ? "bg-white border-emerald-500 shadow-md ring-2 ring-emerald-500/20" 
                : "bg-white border-slate-200 shadow-sm hover:border-emerald-300"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold text-emerald-600 uppercase tracking-wider">Ready for Dispatch</span>
              <CheckCircle2 className="w-4 h-4 text-emerald-500" />
            </div>
            <div className="text-3xl font-extrabold text-emerald-600 mt-2">{stats.ready}</div>
            <div className="text-xs text-emerald-400 mt-1">Validated & Approved</div>
          </div>
        </div>

        {/* Search and Filters Header */}
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 mb-6">
          <div>
            <h2 className="text-xl font-bold text-slate-900 tracking-tight">Active Claims Queue</h2>
            <p className="text-xs text-slate-500 mt-0.5">Showing {filteredCases.length} of {cases.length} patients in queue</p>
          </div>

          <div className="relative w-full sm:w-80">
            <Search className="w-4 h-4 text-slate-400 absolute left-3.5 top-1/2 transform -translate-y-1/2" />
            <input 
              type="text" 
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search patient, MP-ref or ENC ref..." 
              className="w-full pl-10 pr-4 py-2 bg-white border border-slate-300 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 shadow-sm transition-all" 
            />
          </div>
        </div>

        {/* Claims Table Container */}
        <div className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden min-h-[450px] relative">
          {loading ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-slate-400 bg-slate-50/60 backdrop-blur-sm z-10">
              <Loader2 className="w-8 h-8 animate-spin mb-3 text-indigo-600" />
              <p className="text-sm font-semibold tracking-wider text-slate-600">Connecting to Supabase...</p>
            </div>
          ) : error ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-rose-500 bg-rose-50/50 z-10">
              <AlertCircle className="w-8 h-8 mb-3" />
              <p className="text-sm font-semibold">{error}</p>
            </div>
          ) : filteredCases.length === 0 ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-slate-400 z-10">
              <p className="text-sm font-semibold">No cases match your filter</p>
            </div>
          ) : null}

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead className="bg-slate-50/80 border-b border-slate-200">
                <tr>
                  <th className="px-6 py-3.5 text-xs font-bold text-slate-500 uppercase tracking-wider">Patient Details</th>
                  <th className="px-6 py-3.5 text-xs font-bold text-slate-500 uppercase tracking-wider">Identifiers</th>
                  <th className="px-6 py-3.5 text-xs font-bold text-slate-500 uppercase tracking-wider">Stage</th>
                  <th className="px-6 py-3.5 text-xs font-bold text-slate-500 uppercase tracking-wider">Clinical Notes</th>
                  <th className="px-6 py-3.5 text-xs font-bold text-slate-500 uppercase tracking-wider">System Status</th>
                  <th className="px-6 py-3.5"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {filteredCases.map((c) => (
                  <tr 
                    key={c.id} 
                    onClick={() => setSelectedCase(c)} 
                    className="hover:bg-indigo-50/40 cursor-pointer transition-colors group"
                  >
                    <td className="px-6 py-4">
                      <div className="font-semibold text-slate-900 group-hover:text-indigo-600 transition-colors">
                        {c.patient?.name || 'Unknown Patient'}
                      </div>
                      <div className="text-xs text-slate-500 mt-0.5">
                        {c.patient?.gender === 'male' ? 'M' : c.patient?.gender === 'female' ? 'F' : 'O'} • DOB: {c.patient?.birth_date || 'N/A'}
                      </div>
                    </td>

                    <td className="px-6 py-4">
                      <div className="font-mono text-xs font-bold text-slate-800">{c.tracking_ref}</div>
                      <div className="text-xs text-slate-400 mt-0.5">{c.hms_case_ref}</div>
                    </td>

                    <td className="px-6 py-4">
                      <span className="inline-block px-2 py-0.5 rounded text-xs font-semibold uppercase bg-slate-100 text-slate-700">
                        {c.stage}
                      </span>
                    </td>

                    <td className="px-6 py-4 max-w-xs">
                      <div className="text-xs text-slate-600 line-clamp-2 leading-relaxed">
                        {c.raw_clinical_notes || 'No clinical notes recorded.'}
                      </div>
                    </td>

                    <td className="px-6 py-4 whitespace-nowrap">
                      {renderStatusBadge(c.status)}
                    </td>

                    <td className="px-6 py-4 text-right whitespace-nowrap">
                      <ChevronRight className="w-5 h-5 text-slate-300 group-hover:text-indigo-600 transition-colors inline-block" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </main>

      {/* Case Action Modal */}
      {selectedCase && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-sm animate-in fade-in duration-150">
          <div className="bg-white w-full max-w-5xl max-h-[90vh] rounded-2xl shadow-2xl flex flex-col md:flex-row overflow-hidden border border-slate-200">
            
            {/* Left Column: Context & Notes */}
            <div className="w-full md:w-[45%] bg-slate-50 p-6 md:p-8 border-b md:border-b-0 md:border-r border-slate-200 overflow-y-auto">
              <div className="flex items-center justify-between mb-4">
                <span className="bg-indigo-100 text-indigo-800 text-[10px] font-bold px-2.5 py-1 rounded-full uppercase tracking-wider">
                  Case Dossier
                </span>
                <span className="text-xs font-mono font-bold text-slate-400">{selectedCase.tracking_ref}</span>
              </div>

              <h2 className="text-2xl font-extrabold text-slate-900">{selectedCase.patient?.name}</h2>
              <div className="text-xs text-slate-500 mt-1">
                Gender: <span className="font-semibold text-slate-700 capitalize">{selectedCase.patient?.gender}</span> • DOB: <span className="font-semibold text-slate-700">{selectedCase.patient?.birth_date}</span>
              </div>

              <div className="grid grid-cols-2 gap-3 my-6">
                <div className="bg-white p-3.5 rounded-xl border border-slate-200 shadow-sm">
                  <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex items-center">
                    <Hospital className="w-3.5 h-3.5 mr-1" /> Hospital ID
                  </div>
                  <div className="text-xs font-bold text-slate-800 mt-1 truncate">{selectedCase.encounter?.hospital_id}</div>
                </div>

                <div className="bg-white p-3.5 rounded-xl border border-slate-200 shadow-sm">
                  <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex items-center">
                    <Stethoscope className="w-3.5 h-3.5 mr-1" /> Attending
                  </div>
                  <div className="text-xs font-bold text-slate-800 mt-1 truncate">{selectedCase.encounter?.attending_doctor}</div>
                </div>
              </div>

              <div>
                <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">
                  Doctor Clinical Notes
                </label>
                <div className="p-4 bg-amber-50/70 border border-amber-200 rounded-xl text-xs text-slate-800 leading-relaxed font-mono">
                  "{selectedCase.raw_clinical_notes}"
                </div>
              </div>
            </div>

            {/* Right Column: AI Resolution Center */}
            <div className="w-full md:w-[55%] p-6 md:p-8 flex flex-col justify-between overflow-y-auto bg-white relative">
              <button 
                onClick={() => setSelectedCase(null)} 
                className="absolute top-5 right-5 p-1.5 text-slate-400 hover:text-slate-600 hover:bg-slate-100 rounded-lg transition-colors"
              >
                <X className="w-5 h-5"/>
              </button>

              <div>
                <div className="border-b border-slate-100 pb-4 mb-6 flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 className="text-lg font-bold text-slate-900 flex items-center">
                      <Activity className="w-5 h-5 mr-2 text-indigo-600" /> Resolution Engine
                    </h3>
                    <p className="text-xs text-slate-500 mt-0.5">Automated MJPJAY validation and human-in-the-loop actions</p>
                  </div>
                  {/* Manual Dispatch Button (Feature 2) */}
                  {selectedCase.mapped_package_code && selectedCase.status !== 'submitted' && selectedCase.status !== 'payer_approved' && (
                    <button
                      type="button"
                      onClick={handleManualDispatch}
                      disabled={dispatching}
                      className="inline-flex items-center px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-lg text-xs font-semibold shadow-sm transition-all"
                    >
                      {dispatching ? (
                        <><Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" /> Dispatching...</>
                      ) : (
                        <><Send className="w-3.5 h-3.5 mr-1.5" /> Manual Dispatch</>
                      )}
                    </button>
                  )}
                </div>

                {dispatchSuccess && (
                  <div className="mb-4 p-3 bg-emerald-50 border border-emerald-200 rounded-lg text-xs text-emerald-800 flex items-center">
                    <CheckCircle2 className="w-4 h-4 mr-2 text-emerald-600 shrink-0" />
                    {dispatchSuccess}
                  </div>
                )}
                {dispatchError && (
                  <div className="mb-4 p-3 bg-rose-50 border border-rose-200 rounded-lg text-xs text-rose-800 flex items-center">
                    <AlertCircle className="w-4 h-4 mr-2 text-rose-600 shrink-0" />
                    {dispatchError}
                  </div>
                )}

                {/* Section A: Needs Code Confirmation or Action Required without confirmed code */}
                {(selectedCase.status === 'needs_code_confirmation' || (selectedCase.status === 'action_required' && !selectedCase.mapped_package_code)) && selectedCase.alternate_codes && selectedCase.alternate_codes.length > 0 && (
                  <div className="space-y-4">
                    <div className="text-xs font-bold text-slate-500 uppercase tracking-wider">
                      Select Package Code to Confirm
                    </div>

                    {/* Top match card */}
                    <div 
                      onClick={() => setSelectedPackageCode(selectedCase.alternate_codes![0].code)}
                      className={`p-4 border-2 rounded-xl cursor-pointer transition-all ${
                        selectedPackageCode === selectedCase.alternate_codes[0].code
                          ? "border-indigo-600 bg-indigo-50/60 ring-2 ring-indigo-500/10"
                          : "border-slate-200 hover:border-slate-300"
                      }`}
                    >
                      <div className="flex justify-between items-start">
                        <div>
                          <div className="text-[10px] font-bold text-indigo-600 uppercase tracking-wider">AI Top Suggestion</div>
                          <div className="text-sm font-bold text-slate-900 mt-0.5">{selectedCase.alternate_codes[0].name}</div>
                          <div className="text-xs font-mono text-slate-500 mt-0.5">Code: {selectedCase.alternate_codes[0].code}</div>
                        </div>
                        <span className="text-xs font-bold text-emerald-700 bg-emerald-100 px-2.5 py-1 rounded-full">
                          {(selectedCase.alternate_codes[0].confidence * 100).toFixed(1)}% match
                        </span>
                      </div>
                    </div>

                    {/* Alternative candidates */}
                    {selectedCase.alternate_codes.length > 1 && (
                      <div className="space-y-2 pt-2">
                        <div className="text-[11px] font-semibold text-slate-400">Other Candidates:</div>
                        {selectedCase.alternate_codes.slice(1).map((alt, i) => (
                          <div 
                            key={i}
                            onClick={() => setSelectedPackageCode(alt.code)}
                            className={`p-3 border rounded-xl flex items-center justify-between cursor-pointer text-xs transition-all ${
                              selectedPackageCode === alt.code
                                ? "border-indigo-500 bg-indigo-50/40"
                                : "border-slate-200 hover:bg-slate-50"
                            }`}
                          >
                            <div className="flex items-center space-x-3">
                              <input 
                                type="radio" 
                                name="packageCode"
                                checked={selectedPackageCode === alt.code}
                                onChange={() => setSelectedPackageCode(alt.code)}
                                className="w-4 h-4 text-indigo-600 border-slate-300 focus:ring-indigo-500"
                              />
                              <div>
                                <span className="font-medium text-slate-800">{alt.name}</span>
                                <span className="block font-mono text-[10px] text-slate-400">{alt.code}</span>
                              </div>
                            </div>
                            <span className="font-semibold text-slate-500">{(alt.confidence * 100).toFixed(1)}%</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {/* Section B: Action Required / Missing docs */}
                {selectedCase.status === 'action_required' && (() => {
                  const fraudAlerts = (selectedCase.missing_requirements || []).filter(
                    (req) => req.violation_type || (req.human_label && req.human_label.startsWith("FRAUD ALERT"))
                  );
                  const docRequirements = (selectedCase.missing_requirements || []).filter(
                    (req) => !req.violation_type && !(req.human_label && req.human_label.startsWith("FRAUD ALERT"))
                  );

                  return (
                    <div className="space-y-5">
                      {/* B.1 Dedicated Anti-Fraud & Clinical Integrity Violation Banner */}
                      {fraudAlerts.length > 0 && (
                        <div className="p-5 bg-rose-50 border-2 border-rose-400 rounded-xl shadow-sm animate-in fade-in duration-200">
                          <div className="flex items-center text-rose-900 font-extrabold text-sm mb-2">
                            <ShieldAlert className="w-5 h-5 mr-2 text-rose-600 shrink-0" />
                            Anti-Fraud & Clinical Integrity Violation
                          </div>
                          <p className="text-xs text-rose-800 mb-3 leading-relaxed">
                            This claim has been locked by the Automated Clinical Integrity Gatekeeper. The following rule violation was detected:
                          </p>
                          <div className="space-y-2">
                            {fraudAlerts.map((alert, i) => (
                              <div
                                key={i}
                                className="p-3.5 bg-white border border-rose-300 rounded-lg shadow-xs text-xs text-rose-950 flex items-start space-x-2.5"
                              >
                                <Ban className="w-4 h-4 text-rose-600 shrink-0 mt-0.5" />
                                <div className="leading-relaxed font-semibold">
                                  {alert.human_label}
                                </div>
                              </div>
                            ))}
                          </div>
                          <div className="mt-3.5 pt-3 border-t border-rose-200/80 flex items-center justify-between text-[11px] text-rose-700">
                            <span>Actor: <strong className="font-mono">anti_fraud_engine</strong></span>
                            <span className="font-semibold bg-rose-200/80 text-rose-900 px-2.5 py-0.5 rounded-full">
                              Dispatch Blocked
                            </span>
                          </div>
                        </div>
                      )}

                      {/* B.2 Diagnostic Document Upload — Always available in action_required for operators */}
                      <div className="p-5 bg-amber-50/70 border border-amber-200 rounded-xl">
                        <div className="flex items-center text-amber-900 font-bold text-sm mb-2">
                          <AlertCircle className="w-4 h-4 mr-2 text-amber-600" />
                          {docRequirements.length > 0 ? "Pre-Flight Requirements Missing" : "Attach Supporting Clinical Documents"}
                        </div>
                        <p className="text-xs text-amber-800 mb-3 leading-relaxed">
                          {docRequirements.length > 0
                            ? "The pre-flight rule engine flagged the following missing diagnostic documents:"
                            : "Mandatory diagnostic scans, lab reports, or discharge summaries can be uploaded below:"}
                        </p>

                        {docRequirements.length > 0 && (
                          <ul className="mb-4 space-y-2">
                            {docRequirements.map((req, i) => {
                              let reqCode = "";
                              if (req.any_of && req.any_of.length > 0) {
                                const opt = req.any_of[0];
                                reqCode = typeof opt === "string" ? opt : opt.code;
                              }
                              const isSelected = selectedDocType === reqCode;

                              return (
                                <li
                                  key={i}
                                  onClick={() => reqCode && setSelectedDocType(reqCode)}
                                  className={`flex items-center justify-between text-xs rounded-lg px-3 py-2.5 transition-all cursor-pointer border ${
                                    isSelected
                                      ? "bg-amber-100/90 border-amber-400 text-amber-950 shadow-sm font-semibold"
                                      : "bg-white border-amber-200 text-amber-900 hover:bg-amber-100/50"
                                  }`}
                                >
                                  <div className="flex items-start">
                                    <AlertCircle className="w-3.5 h-3.5 mr-2 mt-0.5 text-amber-500 shrink-0" />
                                    <span>{req.human_label}</span>
                                  </div>
                                  {reqCode && (
                                    <span className={`text-[10px] px-2 py-0.5 rounded-full font-mono ${
                                      isSelected
                                        ? "bg-amber-600 text-white font-bold"
                                        : "bg-amber-100 text-amber-800"
                                    }`}>
                                      {isSelected ? "Selected" : "Click to select"}
                                    </span>
                                  )}
                                </li>
                              );
                            })}
                          </ul>
                        )}

                        {/* Document Type Selector & Upload Box */}
                        <div className="bg-white p-4 rounded-xl border border-amber-200 shadow-sm space-y-3">
                          <div>
                            <label className="block text-[11px] font-bold text-slate-700 uppercase tracking-wider mb-1.5">
                              Select Document Type to Fulfill
                            </label>
                            <select
                              value={selectedDocType}
                              onChange={(e) => setSelectedDocType(e.target.value)}
                              className="w-full text-xs bg-slate-50 border border-slate-300 rounded-lg px-3 py-2 font-medium text-slate-800 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:bg-white transition-all"
                            >
                              {docRequirements.length > 0 && (
                                <optgroup label="Missing Requirements">
                                  {docRequirements.map((req, idx) => {
                                    let code = "";
                                    if (req.any_of && req.any_of.length > 0) {
                                      const opt = req.any_of[0];
                                      code = typeof opt === "string" ? opt : opt.code;
                                    }
                                    return (
                                      <option key={idx} value={code || req.human_label}>
                                        {req.human_label} ({code || "custom"})
                                      </option>
                                    );
                                  })}
                                </optgroup>
                              )}
                              <optgroup label="Standard Clinical Taxonomy">
                                <option value="usg_abdomen">Ultrasound Abdomen (usg_abdomen)</option>
                                <option value="clinical_photograph">Clinical Photograph (clinical_photograph)</option>
                                <option value="biopsy_report">Histopathology / Biopsy Report (biopsy_report)</option>
                                <option value="blood_report">Blood / Laboratory Investigation (blood_report)</option>
                                <option value="discharge_summary">Discharge Summary (discharge_summary)</option>
                                <option value="operative_notes">Operative Notes (operative_notes)</option>
                                <option value="xray">X-Ray Imaging (xray)</option>
                                <option value="ct_scan">CT Scan Imaging (ct_scan)</option>
                                <option value="investigation_report">General Investigation Report</option>
                              </optgroup>
                            </select>
                          </div>

                          {/* Hidden Native File Input */}
                          <input
                            type="file"
                            ref={fileInputRef}
                            accept=".pdf,.jpg,.jpeg,.png,.dcm"
                            className="hidden"
                            onChange={(e) => handleFileSelect(e.target.files)}
                          />

                          {/* Dropzone */}
                          <div
                            onDragOver={(e) => {
                              e.preventDefault();
                              setIsDragging(true);
                            }}
                            onDragLeave={() => setIsDragging(false)}
                            onDrop={(e) => {
                              e.preventDefault();
                              setIsDragging(false);
                              handleFileSelect(e.dataTransfer.files);
                            }}
                            onClick={() => fileInputRef.current?.click()}
                            className={`border-2 border-dashed rounded-xl p-5 text-center cursor-pointer transition-all ${
                              isDragging
                                ? "border-indigo-600 bg-indigo-50/80 scale-[1.01]"
                                : uploadFile
                                ? "border-emerald-400 bg-emerald-50/50"
                                : "border-amber-300 hover:border-amber-400 hover:bg-amber-50/40 bg-slate-50/50"
                            }`}
                          >
                            {uploadFile ? (
                              <div className="flex items-center justify-between">
                                <div className="flex items-center space-x-3 text-left">
                                  <div className="w-10 h-10 rounded-lg bg-emerald-100 text-emerald-700 flex items-center justify-center shrink-0">
                                    <FileText className="w-5 h-5" />
                                  </div>
                                  <div className="truncate max-w-[200px] sm:max-w-xs">
                                    <div className="text-xs font-bold text-slate-800 truncate">{uploadFile.name}</div>
                                    <div className="text-[10px] text-slate-500">
                                      {(uploadFile.size / 1024).toFixed(1)} KB • Ready for upload
                                    </div>
                                  </div>
                                </div>
                                <button
                                  type="button"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setUploadFile(null);
                                    if (fileInputRef.current) fileInputRef.current.value = "";
                                  }}
                                  className="p-1.5 text-slate-400 hover:text-rose-600 hover:bg-white rounded-md transition-colors"
                                >
                                  <Trash2 className="w-4 h-4" />
                                </button>
                              </div>
                            ) : (
                              <>
                                <UploadCloud className="w-7 h-7 text-amber-500 mx-auto mb-1.5" />
                                <div className="text-xs font-bold text-amber-800">
                                  Click or drag diagnostic file here (PDF, JPG, PNG)
                                </div>
                                <div className="text-[10px] text-slate-400 mt-0.5">
                                  Select file from local disk to fulfill requirement
                                </div>
                              </>
                            )}
                          </div>

                          {/* Feedback Alerts */}
                          {uploadError && (
                            <div className="p-2.5 rounded-lg bg-rose-100 text-rose-800 text-xs flex items-center">
                              <AlertCircle className="w-4 h-4 mr-1.5 shrink-0 text-rose-600" />
                              <span>{uploadError}</span>
                            </div>
                          )}

                          {uploadSuccess && (
                            <div className="p-2.5 rounded-lg bg-emerald-100 text-emerald-800 text-xs flex items-center">
                              <CheckCircle2 className="w-4 h-4 mr-1.5 shrink-0 text-emerald-600" />
                              <span>{uploadSuccess}</span>
                            </div>
                          )}

                          {/* Staged files queue for Batch Upload (Feature 1) */}
                          {stagedFiles.length > 0 && (
                            <div className="p-3 bg-indigo-50/70 border border-indigo-200 rounded-xl space-y-2.5">
                              <div className="text-[11px] font-bold text-indigo-900 flex items-center justify-between">
                                <span className="flex items-center">
                                  <ListPlus className="w-3.5 h-3.5 mr-1 text-indigo-600" />
                                  Staged Documents for Batch Upload ({stagedFiles.length})
                                </span>
                                <span className="text-[10px] text-indigo-600 font-semibold bg-indigo-100 px-2 py-0.5 rounded-full">
                                  Evaluates once
                                </span>
                              </div>
                              <ul className="space-y-1.5 max-h-36 overflow-y-auto">
                                {stagedFiles.map((staged, i) => (
                                  <li key={i} className="flex items-center justify-between bg-white px-3 py-1.5 rounded-lg border border-indigo-100 text-xs shadow-xs">
                                    <div className="flex items-center space-x-2 truncate">
                                      <span className="px-1.5 py-0.5 text-[10px] font-mono font-semibold bg-indigo-100 text-indigo-800 rounded">
                                        {staged.docType}
                                      </span>
                                      <span className="truncate max-w-[150px] text-slate-700 font-medium">{staged.file.name}</span>
                                    </div>
                                    <button
                                      type="button"
                                      onClick={() => handleRemoveStaged(i)}
                                      className="text-slate-400 hover:text-rose-600 p-0.5"
                                    >
                                      <Trash2 className="w-3.5 h-3.5" />
                                    </button>
                                  </li>
                                ))}
                              </ul>
                              <button
                                type="button"
                                onClick={handleBatchUpload}
                                disabled={uploading}
                                className="w-full py-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-bold rounded-lg shadow-sm text-xs transition-all flex items-center justify-center"
                              >
                                {uploading ? (
                                  <><Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" /> Uploading Batch & Evaluating...</>
                                ) : (
                                  <><UploadCloud className="w-3.5 h-3.5 mr-1.5" /> Upload All {stagedFiles.length} Documents & Evaluate Rules</>
                                )}
                              </button>
                            </div>
                          )}

                          {/* Action Buttons: Stage to Batch Queue OR Single Upload */}
                          <div className="grid grid-cols-2 gap-2 pt-1">
                            <button
                              type="button"
                              onClick={handleStageFile}
                              disabled={uploading || !uploadFile}
                              className="w-full py-2 bg-slate-100 hover:bg-slate-200 border border-slate-300 disabled:opacity-50 text-slate-700 font-bold rounded-lg text-xs transition-all flex items-center justify-center shadow-xs"
                            >
                              <Plus className="w-3.5 h-3.5 mr-1.5 text-indigo-600" /> Stage for Batch
                            </button>
                            <button
                              type="button"
                              onClick={handleUploadDocument}
                              disabled={uploading || !uploadFile}
                              className="w-full py-2 bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-white font-bold rounded-lg text-xs transition-all flex items-center justify-center shadow-xs"
                            >
                              {uploading ? (
                                <><Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" /> Uploading...</>
                              ) : (
                                <><Upload className="w-3.5 h-3.5 mr-1.5" /> Upload Single</>
                              )}
                            </button>
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })()}


                    {/* Attached Case Documents Section */}
                    {caseDocuments.length > 0 && (
                      <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm">
                        <div className="text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-2.5 flex items-center justify-between">
                          <span className="flex items-center">
                            <Paperclip className="w-3.5 h-3.5 mr-1 text-slate-400" /> Attached Case Documents ({caseDocuments.length})
                          </span>
                          {loadingDocs && <Loader2 className="w-3 h-3 animate-spin text-slate-400" />}
                        </div>
                        <ul className="space-y-1.5">
                          {caseDocuments.map((doc) => (
                            <li key={doc.id} className="flex items-center justify-between p-2 rounded-lg bg-slate-50 border border-slate-100 text-xs">
                              <div className="flex items-center space-x-2 truncate">
                                <FileText className="w-3.5 h-3.5 text-indigo-500 shrink-0" />
                                <span className="font-semibold text-slate-800">{doc.document_type}</span>
                                {doc.file_name && (
                                  <span className="text-slate-400 truncate text-[11px]">({doc.file_name})</span>
                                )}
                              </div>
                              {doc.file_url && (
                                <a
                                  href={doc.file_url}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="text-[11px] text-indigo-600 hover:text-indigo-800 flex items-center font-medium ml-2 shrink-0"
                                >
                                  View <ExternalLink className="w-3 h-3 ml-1" />
                                </a>
                              )}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                {/* Section C: Ready for Dispatch */}
                {selectedCase.status === 'ready_for_dispatch' && (
                  <div className="text-center py-8">
                    <div className="w-14 h-14 bg-emerald-100 text-emerald-600 rounded-full flex items-center justify-center mx-auto mb-4">
                      <CheckCircle2 className="w-8 h-8" />
                    </div>
                    <h4 className="text-base font-bold text-slate-900">Pre-Authorization Approved</h4>
                    <p className="text-xs text-slate-500 mt-1 max-w-xs mx-auto">
                      All eligibility criteria, clinical mapping, and pre-flight rules have passed.
                    </p>
                    {selectedCase.mapped_package_code && (
                      <div className="inline-block mt-4 px-3 py-1.5 bg-slate-100 border border-slate-200 rounded-lg text-xs font-mono font-semibold text-slate-800">
                        Package: {selectedCase.mapped_package_code} {selectedCase.mapped_package_name ? `(${selectedCase.mapped_package_name})` : ''}
                      </div>
                    )}
                  </div>
                )}

                {/* Section D: Dispatch Failed */}
                {selectedCase.status === 'dispatch_failed' && (
                  <div className="text-center py-6 p-5 bg-rose-50 border border-rose-200 rounded-xl">
                    <div className="w-12 h-12 bg-rose-100 text-rose-600 rounded-full flex items-center justify-center mx-auto mb-3">
                      <AlertCircle className="w-6 h-6" />
                    </div>
                    <h4 className="text-base font-bold text-rose-900">Payer Dispatch Failed</h4>
                    <p className="text-xs text-rose-700 mt-1 max-w-sm mx-auto">
                      The payer rejected the FHIR bundle or the endpoint was unreachable.
                    </p>
                    {selectedCase.error_message && (
                      <div className="mt-3 p-3 bg-white border border-rose-200 rounded-lg text-xs font-mono text-rose-850 text-left overflow-x-auto">
                        <span className="font-bold block text-[10px] text-rose-500 uppercase tracking-wider mb-1">Reason / Detail:</span>
                        {selectedCase.error_message}
                      </div>
                    )}
                    {selectedCase.mapped_package_code && (
                      <div className="inline-block mt-3 px-3 py-1 bg-white border border-rose-200 rounded-lg text-xs font-mono text-slate-600">
                        Attempted Package: {selectedCase.mapped_package_code}
                      </div>
                    )}
                  </div>
                )}

                {/* Section E: Submitted to Payer */}
                {selectedCase.status === 'submitted' && (
                  <div className="text-center py-8">
                    <div className="w-14 h-14 bg-blue-100 text-blue-600 rounded-full flex items-center justify-center mx-auto mb-4">
                      <UploadCloud className="w-8 h-8" />
                    </div>
                    <h4 className="text-base font-bold text-slate-900">Claim Dispatched & Submitted</h4>
                    <p className="text-xs text-slate-500 mt-1 max-w-xs mx-auto">
                      FHIR R4 Bundle successfully dispatched to the payer scheme.
                    </p>
                    {selectedCase.payer_correlation_id && (
                      <div className="mt-4 inline-block px-3 py-1.5 bg-blue-50 border border-blue-200 rounded-lg text-xs font-mono text-blue-800">
                        Correlation ID: {selectedCase.payer_correlation_id}
                      </div>
                    )}
                  </div>
                )}

                {/* Section F: Payer Approved */}
                {selectedCase.status === 'payer_approved' && (
                  <div className="text-center py-8">
                    <div className="w-14 h-14 bg-emerald-100 text-emerald-600 rounded-full flex items-center justify-center mx-auto mb-4">
                      <CheckCircle2 className="w-8 h-8" />
                    </div>
                    <h4 className="text-base font-bold text-emerald-900">Payer Adjudication Approved</h4>
                    <p className="text-xs text-slate-500 mt-1 max-w-xs mx-auto">
                      Official pre-authorization approval granted by the insurance scheme.
                    </p>
                  </div>
                )}
              </div>

              {/* Bottom Modal Actions */}
              <div className="pt-6 border-t border-slate-100 space-y-3">
                {(selectedCase.status === 'needs_code_confirmation' || (selectedCase.status === 'action_required' && !selectedCase.mapped_package_code && selectedCase.alternate_codes && selectedCase.alternate_codes.length > 0)) && (
                  <button 
                    onClick={handleConfirmCode}
                    disabled={confirming || !selectedPackageCode}
                    className="w-full py-3 bg-indigo-600 hover:bg-indigo-700 text-white font-bold rounded-xl shadow-lg shadow-indigo-500/20 transition-all text-sm disabled:opacity-50 flex items-center justify-center"
                  >
                    {confirming ? (
                      <>
                        <Loader2 className="w-4 h-4 animate-spin mr-2" /> Confirming with Gateway...
                      </>
                    ) : (
                      <>
                        <Check className="w-4 h-4 mr-2" /> Confirm Package & Advance to Rules
                      </>
                    )}
                  </button>
                )}

                {selectedCase.status === 'ready_for_dispatch' && (
                  <div className="flex flex-col sm:flex-row gap-2">
                    <div className="flex-1 py-3 px-4 bg-emerald-50 text-emerald-700 border border-emerald-200 font-semibold rounded-xl text-xs flex items-center justify-center">
                      <Loader2 className="w-4 h-4 mr-2 animate-spin text-emerald-600" /> Auto-Dispatching to NHCX...
                    </div>
                    <button
                      onClick={handleManualDispatch}
                      disabled={dispatching}
                      className="py-3 px-4 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-bold rounded-xl text-xs flex items-center justify-center shadow-sm"
                    >
                      <Send className="w-3.5 h-3.5 mr-1.5" /> Manual Dispatch
                    </button>
                  </div>
                )}

                {selectedCase.status === 'dispatch_failed' && (
                  <button
                    onClick={handleManualDispatch}
                    disabled={dispatching}
                    className="w-full py-3 bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white font-bold rounded-xl text-sm flex items-center justify-center shadow-sm"
                  >
                    {dispatching ? (
                      <><Loader2 className="w-4 h-4 mr-2 animate-spin" /> Retrying Dispatch...</>
                    ) : (
                      <><Send className="w-4 h-4 mr-2" /> Retry Manual Dispatch to NHCX</>
                    )}
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
