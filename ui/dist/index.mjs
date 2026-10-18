import { fmtDateTime as e, useAppApi as t } from "@kirocrew/app-sdk";
import { useMutation as n, useQuery as r, useQueryClient as i } from "@tanstack/react-query";
import { Btn as a, Card as o, CardTitle as s, EmptyState as c, ErrorNotice as l, Input as u, MarkdownRenderer as ee, PageHeader as d, StatCard as f } from "@kirocrew/app-sdk/ui";
import { useEffect as p, useState as m, useSyncExternalStore as te } from "react";
import { Fragment as h, jsx as g, jsxs as _ } from "react/jsx-runtime";
function v(e) {
	return e.some((e) => !["complete", "failed"].includes(e.state));
}
function ne(e, t) {
	let n = e.agent_id === "whisper-meeting-note-taker" ? "notes" : "sketch";
	switch (e.state) {
		case "pending": return e.output_revision ? "Listening" : "Starting";
		case "working": return "Working";
		case "caught_up": return t === "recording" ? "Listening" : "Caught up";
		case "finalizing": return `Finishing ${n}`;
		case "complete": return "Caught up";
		case "unavailable": return "Unavailable";
		case "failed": return "Failed";
		default: return e.state;
	}
}
//#endregion
//#region src/captureQueue.ts
var y = 536870912, b = "whisper-meeting-capture-v1", x = 1, S = "chunks", C = "meta", w = "by-order", T = "byte-total", E = class extends Error {};
function D(e) {
	return new Promise((t, n) => {
		e.onsuccess = () => t(e.result), e.onerror = () => n(e.error ?? /* @__PURE__ */ Error("IndexedDB request failed"));
	});
}
function O(e) {
	return new Promise((t, n) => {
		e.oncomplete = () => t(), e.onabort = () => n(e.error ?? /* @__PURE__ */ Error("IndexedDB transaction aborted")), e.onerror = () => n(e.error ?? /* @__PURE__ */ Error("IndexedDB transaction failed"));
	});
}
function k(e, t, n) {
	return `${e}:${String(t).padStart(8, "0")}:${String(n).padStart(16, "0")}`;
}
var A = class {
	factory;
	capBytes;
	databasePromise;
	constructor(e = indexedDB, t = y) {
		this.factory = e, this.capBytes = t;
	}
	async put(e) {
		let t = (await this.database()).transaction([S, C], "readwrite"), n = O(t), r = t.objectStore(S), i = t.objectStore(C), a = await D(r.get(e.key));
		if (a) {
			if (a.sha256 !== e.sha256 || a.pcm.byteLength !== e.pcm.byteLength) throw t.abort(), await n.catch(() => void 0), Error("browser queue sequence conflict");
			await n;
			return;
		}
		let o = Number(await D(i.get(T)) ?? 0);
		if (o + e.pcm.byteLength > this.capBytes) throw t.abort(), await n.catch(() => void 0), new E("browser audio queue limit reached");
		r.put(e), i.put(o + e.pcm.byteLength, T), await n;
	}
	async first() {
		let e = (await this.database()).transaction(S, "readonly"), t = O(e), n = e.objectStore(S).index(w).openCursor(), r = await new Promise((e, t) => {
			n.onsuccess = () => e(n.result?.value), n.onerror = () => t(n.error ?? /* @__PURE__ */ Error("IndexedDB cursor failed"));
		});
		return await t, r;
	}
	async deleteExact(e) {
		let t = (await this.database()).transaction([S, C], "readwrite"), n = O(t), r = t.objectStore(S), i = t.objectStore(C), a = await D(r.get(e.key));
		if (a?.sha256 === e.sha256) {
			let t = Number(await D(i.get(T)) ?? 0);
			r.delete(e.key), i.put(Math.max(0, t - a.pcm.byteLength), T);
		}
		await n;
	}
	async stats() {
		let e = (await this.database()).transaction([S, C], "readonly"), t = O(e), n = await D(e.objectStore(S).count()), r = Number(await D(e.objectStore(C).get(T)) ?? 0);
		return await t, {
			chunks: n,
			bytes: r
		};
	}
	close() {
		this.databasePromise &&= (this.databasePromise.then((e) => e.close()), void 0);
	}
	database() {
		return this.databasePromise ||= new Promise((e, t) => {
			let n = this.factory.open(b, x);
			n.onupgradeneeded = () => {
				let e = n.result;
				e.createObjectStore(S, { keyPath: "key" }).createIndex(w, "key", { unique: !0 }), e.createObjectStore(C);
			}, n.onsuccess = () => e(n.result), n.onerror = () => t(n.error ?? /* @__PURE__ */ Error("IndexedDB open failed"));
		}), this.databasePromise;
	}
}, j = "/apps/whisper-meeting/api", M = class {
	api;
	queue;
	running = !1;
	rerun = !1;
	activeDrain;
	state = { running: !1 };
	listeners = /* @__PURE__ */ new Set();
	constructor(e, t) {
		this.api = e, this.queue = t;
	}
	subscribe(e) {
		return this.listeners.add(e), e(this.state), () => this.listeners.delete(e);
	}
	async drain() {
		if (this.activeDrain) {
			this.rerun = !0, await this.activeDrain;
			return;
		}
		let e = this.runDrain();
		this.activeDrain = e;
		try {
			await e;
		} finally {
			this.activeDrain === e && (this.activeDrain = void 0);
		}
	}
	async runDrain() {
		this.running = !0, this.publish({
			...this.state,
			running: !0,
			error: void 0
		});
		try {
			do {
				this.rerun = !1;
				let e = await this.queue.first();
				for (; e;) {
					let t = await this.upload(e);
					this.assertAck(e, t), await this.queue.deleteExact(e), this.publish({
						running: !0,
						lastAckSequence: e.sequence
					}), e = await this.queue.first();
				}
			} while (this.rerun);
			this.publish({
				...this.state,
				running: !1,
				error: void 0
			});
		} catch (e) {
			throw this.publish({
				...this.state,
				running: !1,
				error: e instanceof Error ? e.message : String(e)
			}), e;
		} finally {
			this.running = !1;
		}
	}
	async upload(e) {
		let t = `${j}/meetings/${encodeURIComponent(e.meetingId)}/audio/${e.epoch}/${e.sequence}`;
		return (await this.api.request(t, {
			method: "PUT",
			body: e.pcm,
			headers: {
				"Content-Type": "application/octet-stream",
				"X-Chunk-SHA256": e.sha256,
				"X-Sample-Start": String(e.sampleStart),
				"X-Sample-Count": String(e.sampleCount),
				"X-Sample-Rate": String(e.sampleRate),
				"X-Channels": String(e.channels),
				"X-Encoding": e.encoding
			}
		})).ack;
	}
	assertAck(e, t) {
		if (!t.durable || t.meeting_id !== e.meetingId || t.epoch !== e.epoch || t.sequence !== e.sequence || t.sha256 !== e.sha256) throw Error("backend returned a mismatched durable ACK");
	}
	publish(e) {
		this.state = e;
		for (let t of this.listeners) t(e);
	}
}, N = "/apps/whisper-meeting/api", P = "/apps/whisper-meeting/ui/dist/whisper-meeting-worklet.js";
async function F(e) {
	let t = await crypto.subtle.digest("SHA-256", e);
	return Array.from(new Uint8Array(t), (e) => e.toString(16).padStart(2, "0")).join("");
}
var I = new class {
	queue = new A();
	listeners = /* @__PURE__ */ new Set();
	state = {
		phase: "idle",
		queuedChunks: 0,
		queuedBytes: 0
	};
	api;
	uploader;
	unsubscribeUploader;
	stream;
	context;
	source;
	worklet;
	meetingId;
	epoch;
	sequence = 0;
	sampleStart = 0;
	persistChain = Promise.resolve();
	flushComplete;
	subscribe(e) {
		return this.listeners.add(e), e(this.state), () => this.listeners.delete(e);
	}
	snapshot() {
		return this.state;
	}
	async attach(e) {
		this.state.phase !== "recording" && this.ensureUploader(e), await this.refreshQueueStats();
		try {
			await this.uploader?.drain();
		} catch {}
	}
	async start(e, t, n = !1) {
		if (![
			"idle",
			"paused",
			"recovering",
			"error",
			"ended"
		].includes(this.state.phase)) throw Error("capture is already active");
		if (this.ensureUploader(e), this.publish({
			...this.state,
			phase: "starting",
			fatalError: void 0
		}), await this.attach(e), (await this.queue.stats()).chunks > 0) throw this.publish({
			...this.state,
			phase: "error",
			fatalError: "Queued audio must upload before a new capture epoch can start."
		}), Error("queued audio has not drained");
		let r, i;
		try {
			r = await navigator.mediaDevices.getUserMedia({ audio: {
				channelCount: 1,
				echoCancellation: !1,
				noiseSuppression: !1
			} }), i = new AudioContext(), await i.audioWorklet.addModule(P);
			let a = i.createMediaStreamSource(r), o = new AudioWorkletNode(i, "whisper-meeting-pcm"), s = await e.post(`${N}/meetings/${encodeURIComponent(t)}/${n ? "resume" : "start"}`, {});
			this.stream = r, this.context = i, this.source = a, this.worklet = o, this.meetingId = t, this.epoch = s.capture.epoch, this.sequence = 0, this.sampleStart = 0, this.persistChain = Promise.resolve(), o.port.onmessage = (e) => this.onWorkletMessage(e), a.connect(o), this.publish({
				...this.state,
				phase: "recording",
				meetingId: t,
				epoch: s.capture.epoch,
				fatalError: void 0
			});
		} catch (e) {
			throw r?.getTracks().forEach((e) => e.stop()), i && await i.close().catch(() => void 0), this.publish({
				...this.state,
				phase: "error",
				fatalError: e instanceof Error ? e.message : String(e)
			}), e;
		}
	}
	pause(e) {
		return this.finishLocalCapture(e, "pause");
	}
	end(e) {
		return this.finishLocalCapture(e, "end");
	}
	async recover(e, t, n) {
		if (this.state.phase === "recording") throw Error("local capture is still active");
		if (this.ensureUploader(e), this.publish({
			...this.state,
			phase: "uploading",
			meetingId: t,
			fatalError: void 0
		}), await this.uploader?.drain(), (await this.queue.stats()).chunks !== 0) throw Error("queued audio has not drained");
		let r = await e.get(`${N}/meetings/${encodeURIComponent(t)}`);
		if (!r.capture) throw Error("meeting has no capture epoch to drain");
		await this.requestDrain(e, t, n, r.capture);
	}
	markServerState(e, t) {
		this.state.meetingId === e && (t === "paused" || t === "ended") && this.publish({
			...this.state,
			phase: t,
			uploadError: void 0
		});
	}
	async finishLocalCapture(e, t) {
		if (this.state.phase !== "recording" || !this.worklet) return;
		this.publish({
			...this.state,
			phase: "stopping"
		});
		let n = new Promise((e) => {
			this.flushComplete = e;
		});
		this.worklet.port.postMessage({ type: "flush" }), await n, await this.persistChain, this.source?.disconnect(), this.worklet.disconnect(), this.stream?.getTracks().forEach((e) => e.stop()), await this.context?.close().catch(() => void 0), this.clearHardware(), this.publish({
			...this.state,
			phase: "uploading"
		});
		try {
			if (await this.uploader?.drain(), (await this.queue.stats()).chunks !== 0) throw Error("queued audio has not drained");
			if (!this.meetingId || this.epoch === void 0) throw Error("capture identity is missing");
			await this.requestDrain(e, this.meetingId, t, {
				epoch: this.epoch,
				cut_sequence: this.sequence - 1,
				cut_sample: this.sampleStart
			});
		} catch (e) {
			throw this.publish({
				...this.state,
				phase: "recovering",
				fatalError: e instanceof Error ? e.message : String(e)
			}), e;
		}
	}
	async requestDrain(e, t, n, r) {
		this.publish({
			...this.state,
			phase: "transcribing",
			meetingId: t,
			epoch: r.epoch
		}), (await e.post(`${N}/meetings/${encodeURIComponent(t)}/${n}`, r)).operation.state === "complete" && this.publish({
			...this.state,
			phase: n === "pause" ? "paused" : "ended"
		});
	}
	configureUploader(e) {
		this.api = e, this.unsubscribeUploader?.(), this.uploader = new M(e, this.queue), this.unsubscribeUploader = this.uploader.subscribe((e) => this.onUploadState(e));
	}
	ensureUploader(e) {
		(this.api !== e || !this.uploader) && this.configureUploader(e);
	}
	onWorkletMessage(e) {
		if (e.data instanceof ArrayBuffer) {
			let t = e.data;
			this.persistChain = this.persistChain.then(() => this.persist(t)).catch((e) => {
				this.publish({
					...this.state,
					phase: "error",
					fatalError: e instanceof Error ? e.message : String(e)
				}), this.stopHardwareAfterStorageFailure();
			});
		} else e.data?.type === "flushed" && this.flushComplete?.();
	}
	async persist(e) {
		if (!this.meetingId || this.epoch === void 0) throw Error("capture identity is missing");
		let t = e.byteLength / 2, n = {
			key: k(this.meetingId, this.epoch, this.sequence),
			meetingId: this.meetingId,
			epoch: this.epoch,
			sequence: this.sequence,
			capturedAt: (/* @__PURE__ */ new Date()).toISOString(),
			sampleStart: this.sampleStart,
			sampleCount: t,
			sampleRate: 16e3,
			channels: 1,
			encoding: "pcm_s16le",
			sha256: await F(e),
			pcm: e
		};
		await this.queue.put(n), this.sequence += 1, this.sampleStart += t, await this.refreshQueueStats(), this.uploader?.drain().catch(() => void 0);
	}
	async stopHardwareAfterStorageFailure() {
		this.source?.disconnect(), this.worklet?.disconnect(), this.stream?.getTracks().forEach((e) => e.stop()), await this.context?.close().catch(() => void 0), this.clearHardware();
	}
	clearHardware() {
		this.stream = void 0, this.context = void 0, this.source = void 0, this.worklet = void 0, this.flushComplete = void 0;
	}
	onUploadState(e) {
		this.refreshQueueStats(), this.publish({
			...this.state,
			lastAckSequence: e.lastAckSequence ?? this.state.lastAckSequence,
			uploadError: e.error
		});
	}
	async refreshQueueStats() {
		let e = await this.queue.stats();
		this.publish({
			...this.state,
			queuedChunks: e.chunks,
			queuedBytes: e.bytes
		});
	}
	publish(e) {
		this.state = e;
		for (let t of this.listeners) t(e);
	}
}(), re = (e) => I.subscribe(e), ie = () => I.snapshot(), L = "/vendor/mermaid.min.js", R = "script,iframe,frame,object,embed,applet,link,meta,base,form,input,button", z = /* @__PURE__ */ new Set([
	"action",
	"data",
	"formaction",
	"href",
	"poster",
	"src",
	"srcset",
	"xlink:href"
]), B = /(?:@import|expression\s*\(|url\s*\(|javascript:)/i, V = "\n  html { color-scheme: light dark; }\n  body { margin: 0; padding: 16px; font: 14px system-ui; }\n  table { border-collapse: collapse; }\n  td, th { border: 1px solid #888; padding: 6px; }\n  pre { white-space: pre-wrap; }\n  .mermaid { display: flex; justify-content: center; }\n  .mermaid svg { max-width: 100%; height: auto; }\n", H = "(function(){\n  var m = window.mermaid;\n  if (!m) return;\n  function promote(){\n    var nodes = document.querySelectorAll('code.language-mermaid, code.mermaid, pre.mermaid > code');\n    for (var i = 0; i < nodes.length; i++){\n      var code = nodes[i];\n      var host = code.closest('pre') || code;\n      var div = document.createElement('div');\n      div.className = 'mermaid';\n      div.textContent = code.textContent || '';\n      if (host.parentNode) host.parentNode.replaceChild(div, host);\n    }\n  }\n  function start(){\n    try { promote(); } catch (e) {}\n    try { m.initialize({startOnLoad:false,securityLevel:'strict',suppressErrorRendering:true,theme:'default'}); } catch (e) {}\n    try { m.run({querySelector:'.mermaid',suppressErrors:true}); } catch (e) {}\n  }\n  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);\n  else start();\n})();";
function U(e) {
	return [
		"default-src 'none'",
		`script-src 'unsafe-inline' ${`${e}${L}`}`,
		"style-src 'unsafe-inline'",
		"img-src data:",
		"font-src data:",
		"connect-src 'none'",
		"form-action 'none'",
		"base-uri 'none'"
	].join("; ");
}
function W(e) {
	e.querySelectorAll(R).forEach((e) => e.remove()), e.querySelectorAll("*").forEach((e) => {
		for (let t of [...e.attributes]) {
			let n = t.name.toLowerCase();
			(n.startsWith("on") || z.has(n) || n === "style" && B.test(t.value)) && e.removeAttribute(t.name);
		}
	}), e.querySelectorAll("style").forEach((e) => {
		B.test(e.textContent ?? "") && e.remove();
	});
}
function ae(e, t = window.location.origin) {
	let n = new DOMParser().parseFromString(e, "text/html"), r = document.implementation.createHTMLDocument(""), i = r.createDocumentFragment();
	for (let e of [...n.body.childNodes]) i.appendChild(r.importNode(e, !0));
	W(i);
	let a = r.createElement("meta");
	a.httpEquiv = "Content-Security-Policy", a.content = U(t), r.head.prepend(a);
	let o = r.createElement("meta");
	o.setAttribute("charset", "utf-8"), r.head.appendChild(o);
	let s = r.createElement("style");
	s.textContent = V, r.head.appendChild(s);
	let c = r.createElement("script");
	c.src = `${t}${L}`, c.crossOrigin = "anonymous", r.head.appendChild(c), r.body.appendChild(i);
	let l = r.createElement("script");
	return l.textContent = H, r.body.appendChild(l), `<!doctype html>\n${r.documentElement.outerHTML}`;
}
//#endregion
//#region src/transcript.ts
var G = 16e3, K = 30 * G, q = 2 * G, J = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]$/u, Y = /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;
function X(e) {
	let t = Math.floor(e / G), n = Math.floor(t / 60);
	return `${String(n).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}
function Z(e, t) {
	let n = e.trimEnd(), r = t.trimStart();
	return n ? r ? `${n}${J.test(n) && Y.test(r) ? "" : " "}${r}` : n : r;
}
function oe(e) {
	let t = /* @__PURE__ */ new Map();
	for (let n of e) t.set(n.epoch, Math.max(t.get(n.epoch) ?? 0, n.audio_end_sample));
	let n = /* @__PURE__ */ new Map(), r = 0;
	for (let e of [...t.keys()].sort((e, t) => e - t)) n.set(e, r), r += t.get(e) ?? 0;
	let i = [], a, o, s = () => {
		a &&= (i.push({
			kind: "speech",
			key: a.key,
			text: a.text,
			timestamp: a.timestamp
		}), void 0);
	};
	for (let t of e) {
		if (t.coverage_kind === "empty_decode") continue;
		if (t.coverage_kind !== "speech") {
			s(), i.push({
				kind: "silence",
				key: String(t.id),
				segment: t
			});
			continue;
		}
		let e = (n.get(t.epoch) ?? 0) + t.audio_start_sample, r = Math.floor(e / K);
		a !== void 0 && a.epoch === t.epoch && a.bucket === r && t.audio_start_sample - a.lastEnd <= q && a ? (a.text = Z(a.text, t.text), a.lastEnd = Math.max(a.lastEnd, t.audio_end_sample), o = r) : (s(), a = {
			key: String(t.id),
			text: t.text.trim(),
			timestamp: r === o ? void 0 : X(e),
			epoch: t.epoch,
			bucket: r,
			lastEnd: t.audio_end_sample
		}, o = r);
	}
	return s(), i;
}
//#endregion
//#region src/App.tsx
var Q = "/apps/whisper-meeting/api";
function $() {
	let y = t(), b = i(), [x, S] = m(""), [C, w] = m(), [T, E] = m(""), [D, O] = m(), [k, A] = m(), [j, M] = m("notes"), [N, P] = m(""), F = te(re, ie);
	p(() => {
		I.attach(y);
	}, [y]), p(() => {
		let e = (e) => {
			(F.phase === "recording" || F.queuedChunks > 0) && e.preventDefault();
		};
		return window.addEventListener("beforeunload", e), () => window.removeEventListener("beforeunload", e);
	}, [F.phase, F.queuedChunks]);
	let L = r({
		queryKey: ["meetings"],
		queryFn: () => y.get(`${Q}/meetings`)
	}), R = r({
		queryKey: ["whisper-meeting-model"],
		queryFn: () => y.get(`${Q}/model`),
		refetchInterval: (e) => e.state.data?.model.status === "installing" && 1e3
	}), z = r({
		queryKey: ["meeting", C],
		queryFn: () => y.get(`${Q}/meetings/${encodeURIComponent(C ?? "")}`),
		enabled: !!C
	}), B = r({
		queryKey: ["meeting-transcript", C],
		queryFn: () => y.get(`${Q}/meetings/${encodeURIComponent(C ?? "")}/transcript`),
		enabled: !!C
	}), V = r({
		queryKey: ["meeting-agents", C],
		queryFn: () => y.get(`${Q}/meetings/${encodeURIComponent(C ?? "")}/agents`),
		enabled: !!C,
		refetchInterval: (e) => v(e.state.data?.agents ?? []) ? 1e3 : !1
	}), H = n({
		mutationFn: () => y.post(`${Q}/model/install`, {}),
		onSuccess: async () => {
			await b.invalidateQueries({ queryKey: ["whisper-meeting-model"] });
		}
	}), U = n({
		mutationFn: (e) => y.post(`${Q}/meetings`, { title: e }),
		onSuccess: async () => {
			S(""), await b.invalidateQueries({ queryKey: ["meetings"] });
		}
	}), W = n({
		mutationFn: (e) => y.post(`${Q}/meetings/${encodeURIComponent(C ?? "")}/context`, { text: e }),
		onSuccess: async () => {
			P(""), await b.invalidateQueries({ queryKey: ["meeting-agents", C] });
		}
	});
	p(() => {
		if (!C) return;
		let e = new EventSource(`${Q}/meetings/${encodeURIComponent(C)}/events`);
		return e.onmessage = () => {
			b.invalidateQueries({ queryKey: ["meetings"] }), b.invalidateQueries({ queryKey: ["meeting", C] }), b.invalidateQueries({ queryKey: ["meeting-transcript", C] }), b.invalidateQueries({ queryKey: ["meeting-agents", C] });
		}, () => e.close();
	}, [b, C]), p(() => {
		z.data && I.markServerState(z.data.meeting.id, z.data.meeting.state);
	}, [z.data]);
	async function G(e, t) {
		w(e), E("");
		try {
			await t(), await Promise.all([
				b.invalidateQueries({ queryKey: ["meetings"] }),
				b.invalidateQueries({ queryKey: ["meeting", e] }),
				b.invalidateQueries({ queryKey: ["meeting-transcript", e] }),
				b.invalidateQueries({ queryKey: ["meeting-agents", e] })
			]);
		} catch (e) {
			E(e instanceof Error ? e.message : String(e));
		}
	}
	async function K(e, t) {
		E("");
		try {
			let n = await y.raw(`${Q}/meetings/${encodeURIComponent(e)}/export?format=${t}`), r = (n.headers.get("Content-Disposition") ?? "").match(/filename="([^"]+)"/)?.[1] ?? `meeting.${t === "text" ? "txt" : t === "markdown" ? "md" : "json"}`, i = URL.createObjectURL(await n.blob()), a = document.createElement("a");
			a.href = i, a.download = r, a.click(), URL.revokeObjectURL(i);
		} catch (e) {
			E(e instanceof Error ? e.message : String(e));
		}
	}
	async function q(e) {
		let t = e.title || "Untitled meeting";
		if (window.confirm(`Permanently delete “${t}” and its local data?`)) {
			E("");
			try {
				await y.del(`${Q}/meetings/${encodeURIComponent(e.id)}`), C === e.id && w(void 0), await b.invalidateQueries({ queryKey: ["meetings"] });
			} catch (e) {
				E(e instanceof Error ? e.message : String(e));
			}
		}
	}
	async function J(e) {
		if (C) {
			E(""), O(e);
			try {
				await y.post(`${Q}/meetings/${encodeURIComponent(C)}/segments/${e}/retry`, {}), await Promise.all([b.invalidateQueries({ queryKey: ["meeting", C] }), b.invalidateQueries({ queryKey: ["meeting-transcript", C] })]);
			} catch (e) {
				E(e instanceof Error ? e.message : String(e));
			} finally {
				O(void 0);
			}
		}
	}
	async function Y(e) {
		if (C) {
			E(""), A(e);
			try {
				await y.post(`${Q}/meetings/${encodeURIComponent(C)}/agents/${encodeURIComponent(e)}/retry`, {}), await b.invalidateQueries({ queryKey: ["meeting-agents", C] });
			} catch (e) {
				E(e instanceof Error ? e.message : String(e));
			} finally {
				A(void 0);
			}
		}
	}
	let X = L.data?.meetings ?? [], Z = X.find((e) => e.last_error), $ = L.error ?? R.error ?? U.error ?? H.error ?? z.error ?? B.error ?? V.error ?? W.error, se = $ instanceof Error ? $.message : String($ ?? "");
	return /* @__PURE__ */ _(h, { children: [/* @__PURE__ */ g(d, {
		title: "Whisper Meeting",
		subtitle: "Durable, local-first meeting transcription"
	}), /* @__PURE__ */ _("div", {
		className: "px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0",
		children: [
			/* @__PURE__ */ _("div", {
				className: "grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6",
				children: [
					/* @__PURE__ */ g(f, {
						label: "Meetings",
						value: String(X.length),
						accent: !0
					}),
					/* @__PURE__ */ g(f, {
						label: "Storage",
						value: "Local"
					}),
					/* @__PURE__ */ g(f, {
						label: "Whisper model",
						value: R.data?.model.status ?? "Checking"
					}),
					/* @__PURE__ */ g(f, {
						label: "Capture",
						value: F.phase
					}),
					/* @__PURE__ */ g(f, {
						label: "Upload queue",
						value: String(F.queuedChunks)
					})
				]
			}),
			$ ? /* @__PURE__ */ g("div", {
				className: "mb-4",
				children: /* @__PURE__ */ g(l, {
					title: "Whisper Meeting could not load",
					message: se
				})
			}) : null,
			F.fatalError || F.uploadError ? /* @__PURE__ */ g("div", {
				className: "mb-4",
				children: /* @__PURE__ */ g(l, {
					title: F.fatalError ? "Capture stopped" : "Upload will retry",
					message: F.fatalError ?? F.uploadError
				})
			}) : null,
			Z ? /* @__PURE__ */ g("div", {
				className: "mb-4",
				children: /* @__PURE__ */ g(l, {
					title: "Recognition is waiting to recover",
					message: `${Z.title || "Untitled meeting"}: ${Z.last_error}`
				})
			}) : null,
			T ? /* @__PURE__ */ g("div", {
				className: "mb-4",
				children: /* @__PURE__ */ g(l, {
					title: "Meeting action failed",
					message: T
				})
			}) : null,
			F.phase === "recording" ? /* @__PURE__ */ _(o, {
				className: "mb-4",
				children: [
					/* @__PURE__ */ g(s, { children: "Recording locally" }),
					/* @__PURE__ */ g("p", {
						className: "text-sm text-muted mt-2",
						children: "Audio is committed to IndexedDB before upload. Closing this page does not stop the module-level capture engine."
					}),
					/* @__PURE__ */ _("div", {
						className: "mt-3 flex gap-2",
						children: [/* @__PURE__ */ g(a, {
							onClick: () => {
								F.meetingId && G(F.meetingId, () => I.pause(y));
							},
							children: "Pause"
						}), /* @__PURE__ */ g(a, {
							onClick: () => {
								F.meetingId && G(F.meetingId, () => I.end(y));
							},
							children: "End"
						})]
					})
				]
			}) : null,
			[
				"stopping",
				"uploading",
				"transcribing",
				"recovering"
			].includes(F.phase) ? /* @__PURE__ */ _(o, {
				className: "mb-4",
				children: [/* @__PURE__ */ g(s, { children: "Finishing the durable cut" }), /* @__PURE__ */ g("p", {
					className: "text-sm text-muted mt-2",
					children: F.phase === "transcribing" ? "All captured audio is durable. Waiting for final transcript coverage." : "Keeping the meeting open until every local chunk has a durable backend ACK."
				})]
			}) : null,
			R.data?.model.status === "ready" ? null : /* @__PURE__ */ _(o, {
				className: "mb-4",
				children: [
					/* @__PURE__ */ g(s, { children: "Local Whisper model" }),
					/* @__PURE__ */ g("p", {
						className: "text-sm text-muted mt-2",
						children: "Whisper Meeting keeps its own verified large-v3-turbo model so Kiro Crew updates cannot replace it."
					}),
					R.data?.model.error ? /* @__PURE__ */ g("p", {
						className: "text-sm text-danger mt-2",
						children: R.data.model.error
					}) : null,
					/* @__PURE__ */ g("div", {
						className: "mt-3",
						children: /* @__PURE__ */ g(a, {
							onClick: () => H.mutate(),
							disabled: R.data?.model.status === "installing" || H.isPending,
							children: R.data?.model.status === "installing" ? "Installing and testing…" : "Install local model"
						})
					})
				]
			}),
			/* @__PURE__ */ _(o, {
				className: "mb-4",
				children: [/* @__PURE__ */ g(s, { children: "New meeting" }), /* @__PURE__ */ _("div", {
					className: "flex flex-col md:flex-row gap-3 mt-3",
					children: [/* @__PURE__ */ g(u, {
						value: x,
						onChange: (e) => S(e.target.value),
						placeholder: "Optional meeting title",
						"aria-label": "Meeting title",
						maxLength: 200
					}), /* @__PURE__ */ g(a, {
						onClick: () => U.mutate(x),
						disabled: U.isPending,
						children: U.isPending ? "Creating…" : "Create meeting"
					})]
				})]
			}),
			/* @__PURE__ */ _(o, { children: [/* @__PURE__ */ g(s, { children: "Meeting history" }), L.isPending ? /* @__PURE__ */ g("p", {
				className: "text-sm text-muted mt-3",
				children: "Loading meetings…"
			}) : X.length === 0 ? /* @__PURE__ */ g(c, {
				icon: /* @__PURE__ */ g("span", {
					"aria-hidden": "true",
					children: "🎙️"
				}),
				title: "No meetings yet",
				subtitle: "Create a meeting to verify the durable app store."
			}) : /* @__PURE__ */ g("div", {
				className: "divide-y divide-border mt-3",
				children: X.map((t) => /* @__PURE__ */ _("div", {
					className: "py-3 first:pt-0 last:pb-0",
					children: [/* @__PURE__ */ _("div", {
						className: "flex items-center justify-between gap-3",
						children: [/* @__PURE__ */ g("div", {
							className: "font-medium text-text",
							children: t.title || "Untitled meeting"
						}), /* @__PURE__ */ _("div", {
							className: "flex gap-2",
							children: [
								/* @__PURE__ */ g(a, {
									onClick: () => w(t.id),
									children: "View"
								}),
								t.state === "draft" && F.phase !== "recording" ? /* @__PURE__ */ g(a, {
									onClick: () => void G(t.id, () => I.start(y, t.id)),
									disabled: F.phase === "starting" || R.data?.model.status !== "ready",
									children: "Record"
								}) : null,
								t.state === "paused" && F.phase !== "recording" ? /* @__PURE__ */ _(h, { children: [/* @__PURE__ */ g(a, {
									onClick: () => void G(t.id, () => I.start(y, t.id, !0)),
									disabled: F.phase === "starting" || R.data?.model.status !== "ready",
									children: "Resume"
								}), /* @__PURE__ */ g(a, {
									onClick: () => void G(t.id, () => I.recover(y, t.id, "end")),
									children: "End"
								})] }) : null,
								t.state === "recording" && F.meetingId !== t.id ? /* @__PURE__ */ g(a, {
									onClick: () => void G(t.id, () => I.recover(y, t.id, "pause")),
									children: "Recover & pause"
								}) : null,
								["recording", "draining"].includes(t.state) ? null : /* @__PURE__ */ g(a, {
									onClick: () => void q(t),
									children: "Delete"
								})
							]
						})]
					}), /* @__PURE__ */ _("div", {
						className: "text-sm text-muted mt-1",
						children: [
							t.state,
							" · ",
							e(t.created_at)
						]
					})]
				}, t.id))
			})] }),
			C ? /* @__PURE__ */ _(o, {
				className: "mt-4",
				children: [
					/* @__PURE__ */ _("div", {
						className: "flex flex-wrap items-start justify-between gap-3",
						children: [/* @__PURE__ */ _("div", { children: [/* @__PURE__ */ g(s, { children: "Meeting workspace" }), z.data ? /* @__PURE__ */ _("p", {
							className: "text-sm text-muted mt-2",
							children: [
								z.data.meeting.state,
								" · ",
								z.data.progress.transcribed_chunks,
								"/",
								z.data.progress.chunks,
								" chunks covered",
								` · ${z.data.progress.pending_chunks} awaiting recognition`,
								z.data.operation ? ` · ${z.data.operation.kind} ${z.data.operation.state}` : ""
							]
						}) : null] }), /* @__PURE__ */ g("div", {
							className: "flex flex-wrap gap-2 text-xs text-muted",
							children: (V.data?.agents ?? []).map((e) => /* @__PURE__ */ _("span", {
								className: "rounded-full border border-border px-2 py-1",
								children: [
									e.agent_id === "whisper-meeting-note-taker" ? "Note Taker" : "Sketch Artist",
									":",
									" ",
									ne(e, z.data?.meeting.state),
									(() => {
										let t = (V.data?.dispatch_counts ?? []).filter((t) => t.agent_id === e.agent_id && t.state === "pending").reduce((e, t) => e + t.count, 0);
										return t ? ` · ${t} queued` : "";
									})()
								]
							}, e.agent_id))
						})]
					}),
					(V.data?.agents ?? []).filter((e) => e.state === "failed").map((e) => /* @__PURE__ */ _("div", {
						className: "mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-danger/40 px-3 py-2",
						children: [/* @__PURE__ */ _("p", {
							className: "text-sm text-danger",
							children: [
								e.agent_id === "whisper-meeting-note-taker" ? "Note Taker" : "Sketch Artist",
								" failed",
								e.last_error ? `: ${e.last_error}` : ".",
								" ",
								"Transcription is unaffected."
							]
						}), /* @__PURE__ */ g(a, {
							onClick: () => void Y(e.agent_id),
							disabled: k !== void 0,
							children: k === e.agent_id ? "Retrying…" : "Retry agent"
						})]
					}, `${e.agent_id}-failure`)),
					z.data?.meeting.last_error ? /* @__PURE__ */ _("p", {
						className: "text-sm text-danger mt-2",
						children: ["Recognition will retry without deleting audio: ", z.data.meeting.last_error]
					}) : null,
					/* @__PURE__ */ _("div", {
						className: "mt-4 grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.9fr)]",
						children: [/* @__PURE__ */ _("section", {
							className: "min-w-0",
							children: [/* @__PURE__ */ _("div", {
								className: "flex gap-2 border-b border-border pb-2",
								children: [
									/* @__PURE__ */ g(a, {
										onClick: () => M("notes"),
										disabled: j === "notes",
										children: "Notes"
									}),
									/* @__PURE__ */ g(a, {
										onClick: () => M("sketch"),
										disabled: j === "sketch",
										children: "Sketch"
									}),
									/* @__PURE__ */ g(a, {
										onClick: () => void K(C, "notes"),
										children: "Export notes"
									}),
									/* @__PURE__ */ g(a, {
										onClick: () => void K(C, "sketch"),
										children: "Export sketch"
									}),
									/* @__PURE__ */ g(a, {
										onClick: () => void K(C, "combined"),
										children: "Combined"
									})
								]
							}), /* @__PURE__ */ g("div", {
								className: "mt-3 min-h-64 rounded-md border border-border p-4",
								children: j === "notes" ? /* @__PURE__ */ g(ee, { content: V.data?.outputs["whisper-meeting-note-taker"] ?? "_Notes pending._" }) : /* @__PURE__ */ _("div", { children: [/* @__PURE__ */ g("p", {
									className: "text-xs text-muted mb-3",
									children: "Sketch output is isolated with scripts, navigation, and network access disabled."
								}), /* @__PURE__ */ g("iframe", {
									title: "Meeting sketch",
									sandbox: "allow-scripts",
									srcDoc: ae(V.data?.outputs["whisper-meeting-sketch-artist"] ?? "<p>Sketch pending.</p>"),
									className: "h-[30rem] w-full rounded-md border border-border bg-surface"
								})] })
							})]
						}), /* @__PURE__ */ _("section", {
							className: "min-w-0 lg:border-l lg:border-border lg:pl-5",
							children: [/* @__PURE__ */ _("div", {
								className: "flex flex-wrap items-center justify-between gap-2",
								children: [/* @__PURE__ */ g("h3", {
									className: "font-medium text-text",
									children: "Transcript"
								}), /* @__PURE__ */ _("div", {
									className: "flex flex-wrap gap-2",
									children: [
										/* @__PURE__ */ g(a, {
											onClick: () => void K(C, "markdown"),
											children: "Markdown"
										}),
										/* @__PURE__ */ g(a, {
											onClick: () => void K(C, "text"),
											children: "Text"
										}),
										/* @__PURE__ */ g(a, {
											onClick: () => void K(C, "json"),
											children: "JSON"
										})
									]
								})]
							}), /* @__PURE__ */ _("div", {
								className: "mt-3 max-h-[32rem] space-y-2 overflow-y-auto pr-2",
								children: [oe(B.data?.segments ?? []).map((e) => e.kind === "speech" ? /* @__PURE__ */ _("div", { children: [e.timestamp ? /* @__PURE__ */ g("div", {
									className: "mb-1 mt-4 text-xs font-medium tabular-nums text-muted",
									children: e.timestamp
								}) : null, /* @__PURE__ */ g("p", {
									className: "text-sm leading-relaxed text-text",
									children: e.text
								})] }, e.key) : /* @__PURE__ */ _("div", {
									className: "flex items-center justify-between gap-3 rounded-md bg-muted/20 px-3 py-2",
									children: [/* @__PURE__ */ _("span", {
										className: "text-xs text-muted",
										children: [
											"Silence covered",
											" · ",
											(e.segment.audio_start_sample / 16e3).toFixed(1),
											"–",
											(e.segment.audio_end_sample / 16e3).toFixed(1),
											"s"
										]
									}), /* @__PURE__ */ g(a, {
										onClick: () => void J(e.segment.id),
										disabled: D !== void 0,
										children: D === e.segment.id ? "Retrying…" : "Recognize again"
									})]
								}, e.key)), B.data && B.data.segments.length === 0 ? /* @__PURE__ */ g("p", {
									className: "text-sm text-muted",
									children: "No committed speech yet."
								}) : null]
							})]
						})]
					}),
					/* @__PURE__ */ _("section", {
						className: "mt-5 border-t border-border pt-4",
						children: [
							/* @__PURE__ */ g("h3", {
								className: "font-medium text-text",
								children: "Meeting Chat"
							}),
							/* @__PURE__ */ g("p", {
								className: "mt-1 text-xs text-muted",
								children: "Add participants, agenda, terminology, or corrections. Messages go to both agents and never rewrite the raw transcript."
							}),
							(V.data?.context ?? []).length > 0 ? /* @__PURE__ */ g("div", {
								className: "mt-3 max-h-32 space-y-2 overflow-y-auto",
								children: V.data?.context.map((t) => /* @__PURE__ */ _("div", {
									className: "rounded-md bg-muted/20 px-3 py-2 text-sm",
									children: [/* @__PURE__ */ _("span", {
										className: "mr-2 text-xs text-muted",
										children: ["Typed · ", e(t.created_at)]
									}), t.text]
								}, t.id))
							}) : null,
							/* @__PURE__ */ _("div", {
								className: "mt-3 flex flex-col gap-2 md:flex-row",
								children: [/* @__PURE__ */ g(u, {
									value: N,
									onChange: (e) => P(e.target.value),
									placeholder: "Tell the meeting participants, agenda, or a correction…",
									"aria-label": "Meeting context",
									maxLength: 4e3,
									disabled: z.data?.meeting.state !== "recording"
								}), /* @__PURE__ */ g(a, {
									onClick: () => W.mutate(N.trim()),
									disabled: z.data?.meeting.state !== "recording" || !N.trim() || W.isPending,
									children: W.isPending ? "Sending…" : "Send"
								})]
							}),
							z.data?.meeting.state === "recording" ? null : /* @__PURE__ */ g("p", {
								className: "mt-2 text-xs text-muted",
								children: "Meeting Chat is available while recording."
							})
						]
					})
				]
			}) : null
		]
	})] });
}
//#endregion
export { $ as default };
