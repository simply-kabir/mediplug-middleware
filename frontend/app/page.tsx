"use client";

import { useState, useEffect } from "react";
import { createClient } from "@supabase/supabase-js";
import { 
  Search, AlertCircle, Clock, CheckCircle2, UploadCloud, 
  Activity, X, ChevronRight, ShieldCheck, Loader2,
  FileText, Check, Hospital, UserCheck, Stethoscope, RefreshCw
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
  missing_requirements?: { human_label: string; satisfied: boolean }[];
  mapped_package_code?: string;
  mapped_package_name?: string;
  confidence?: number;
  alternate_codes?: { code: string; name: string; confidence: number }[];
  payer_correlation_id?: string;
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
                <div className="border-b border-slate-100 pb-4 mb-6">
                  <h3 className="text-lg font-bold text-slate-900 flex items-center">
                    <Activity className="w-5 h-5 mr-2 text-indigo-600" /> Resolution Engine
                  </h3>
                  <p className="text-xs text-slate-500 mt-0.5">Automated MJPJAY validation and human-in-the-loop actions</p>
                </div>

                {/* Section A: Needs Code Confirmation */}
                {selectedCase.status === 'needs_code_confirmation' && selectedCase.alternate_codes && selectedCase.alternate_codes.length > 0 && (
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
                {selectedCase.status === 'action_required' && (
                  <div className="p-5 bg-rose-50 border border-rose-200 rounded-xl">
                    <div className="flex items-center text-rose-800 font-bold text-sm mb-2">
                      <AlertCircle className="w-4 h-4 mr-2 text-rose-600" /> Pre-Flight Requirements Missing
                    </div>
                    <p className="text-xs text-rose-700 mb-3 leading-relaxed">
                      This claim requires human intervention. The pre-flight rule engine flagged the following unmet requirements:
                    </p>
                    {selectedCase.missing_requirements && selectedCase.missing_requirements.length > 0 ? (
                      <ul className="mb-4 space-y-1.5">
                        {selectedCase.missing_requirements.map((req, i) => (
                          <li key={i} className="flex items-start text-xs text-rose-800 bg-white border border-rose-200 rounded-lg px-3 py-2">
                            <AlertCircle className="w-3.5 h-3.5 mr-2 mt-0.5 text-rose-500 shrink-0" />
                            <span className="font-medium">{req.human_label}</span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-xs text-rose-700 mb-4 italic">
                        Mandatory diagnostic scan or investigation report is missing from the record.
                      </p>
                    )}
                    <div className="border-2 border-dashed border-rose-300 bg-white rounded-xl p-6 text-center cursor-pointer hover:bg-rose-50/50 transition-colors">
                      <UploadCloud className="w-8 h-8 text-rose-400 mx-auto mb-2" />
                      <div className="text-xs font-bold text-rose-600">Upload Missing Diagnostic Scan (PDF, JPG)</div>
                      <div className="text-[10px] text-slate-400 mt-1">Pre-flight rule check will automatically re-trigger</div>
                    </div>
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
              </div>

              {/* Bottom Modal Actions */}
              <div className="pt-6 border-t border-slate-100">
                {selectedCase.status === 'needs_code_confirmation' && (
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
                        <Check className="w-4 h-4 mr-2" /> Confirm Package & Advance to Dispatch
                      </>
                    )}
                  </button>
                )}

                {selectedCase.status === 'ready_for_dispatch' && (
                  <button className="w-full py-3 bg-emerald-600 hover:bg-emerald-700 text-white font-bold rounded-xl shadow-lg shadow-emerald-500/20 transition-all text-sm flex items-center justify-center">
                    Transmit FHIR Bundle to NHCX Gateway
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
