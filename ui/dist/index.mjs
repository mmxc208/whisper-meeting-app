import { fmtDateTime as e, useAppApi as t } from "@kirocrew/app-sdk";
import { useMutation as n, useQuery as r, useQueryClient as i } from "@tanstack/react-query";
import { Btn as a, Card as o, CardTitle as s, EmptyState as c, ErrorNotice as l, Input as u, MarkdownRenderer as ee, PageHeader as te, StatCard as d } from "@kirocrew/app-sdk/ui";
import { useEffect as f, useState as p, useSyncExternalStore as ne } from "react";
import { Fragment as m, jsx as h, jsxs as g } from "react/jsx-runtime";
function re(e) {
	return e.some((e) => !["complete", "failed"].includes(e.state));
}
function ie(e, t) {
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
var _ = 536870912, v = "whisper-meeting-capture-v1", y = 1, b = "chunks", x = "meta", S = "by-order", C = "byte-total", w = class extends Error {};
function T(e) {
	return new Promise((t, n) => {
		e.onsuccess = () => t(e.result), e.onerror = () => n(e.error ?? /* @__PURE__ */ Error("IndexedDB request failed"));
	});
}
function E(e) {
	return new Promise((t, n) => {
		e.oncomplete = () => t(), e.onabort = () => n(e.error ?? /* @__PURE__ */ Error("IndexedDB transaction aborted")), e.onerror = () => n(e.error ?? /* @__PURE__ */ Error("IndexedDB transaction failed"));
	});
}
function D(e, t, n) {
	return `${e}:${String(t).padStart(8, "0")}:${String(n).padStart(16, "0")}`;
}
var O = class {
	factory;
	capBytes;
	databasePromise;
	constructor(e = indexedDB, t = _) {
		this.factory = e, this.capBytes = t;
	}
	async put(e) {
		let t = (await this.database()).transaction([b, x], "readwrite"), n = E(t), r = t.objectStore(b), i = t.objectStore(x), a = await T(r.get(e.key));
		if (a) {
			if (a.sha256 !== e.sha256 || a.pcm.byteLength !== e.pcm.byteLength) throw t.abort(), await n.catch(() => void 0), Error("browser queue sequence conflict");
			await n;
			return;
		}
		let o = Number(await T(i.get(C)) ?? 0);
		if (o + e.pcm.byteLength > this.capBytes) throw t.abort(), await n.catch(() => void 0), new w("browser audio queue limit reached");
		r.put(e), i.put(o + e.pcm.byteLength, C), await n;
	}
	async first() {
		let e = (await this.database()).transaction(b, "readonly"), t = E(e), n = e.objectStore(b).index(S).openCursor(), r = await new Promise((e, t) => {
			n.onsuccess = () => e(n.result?.value), n.onerror = () => t(n.error ?? /* @__PURE__ */ Error("IndexedDB cursor failed"));
		});
		return await t, r;
	}
	async deleteExact(e) {
		let t = (await this.database()).transaction([b, x], "readwrite"), n = E(t), r = t.objectStore(b), i = t.objectStore(x), a = await T(r.get(e.key));
		if (a?.sha256 === e.sha256) {
			let t = Number(await T(i.get(C)) ?? 0);
			r.delete(e.key), i.put(Math.max(0, t - a.pcm.byteLength), C);
		}
		await n;
	}
	async stats() {
		let e = (await this.database()).transaction([b, x], "readonly"), t = E(e), n = await T(e.objectStore(b).count()), r = Number(await T(e.objectStore(x).get(C)) ?? 0);
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
			let n = this.factory.open(v, y);
			n.onupgradeneeded = () => {
				let e = n.result;
				e.createObjectStore(b, { keyPath: "key" }).createIndex(S, "key", { unique: !0 }), e.createObjectStore(x);
			}, n.onsuccess = () => e(n.result), n.onerror = () => t(n.error ?? /* @__PURE__ */ Error("IndexedDB open failed"));
		}), this.databasePromise;
	}
}, k = "/apps/whisper-meeting/api", A = class {
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
		let t = `${k}/meetings/${encodeURIComponent(e.meetingId)}/audio/${e.epoch}/${e.sequence}`;
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
}, j = "/apps/whisper-meeting/api", M = "/apps/whisper-meeting/ui/dist/whisper-meeting-worklet.js";
async function N(e) {
	let t = await crypto.subtle.digest("SHA-256", e);
	return Array.from(new Uint8Array(t), (e) => e.toString(16).padStart(2, "0")).join("");
}
var P = new class {
	queue = new O();
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
			} }), i = new AudioContext(), await i.audioWorklet.addModule(M);
			let a = i.createMediaStreamSource(r), o = new AudioWorkletNode(i, "whisper-meeting-pcm"), s = await e.post(`${j}/meetings/${encodeURIComponent(t)}/${n ? "resume" : "start"}`, {});
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
		let r = await e.get(`${j}/meetings/${encodeURIComponent(t)}`);
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
		}), (await e.post(`${j}/meetings/${encodeURIComponent(t)}/${n}`, r)).operation.state === "complete" && this.publish({
			...this.state,
			phase: n === "pause" ? "paused" : "ended"
		});
	}
	configureUploader(e) {
		this.api = e, this.unsubscribeUploader?.(), this.uploader = new A(e, this.queue), this.unsubscribeUploader = this.uploader.subscribe((e) => this.onUploadState(e));
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
			key: D(this.meetingId, this.epoch, this.sequence),
			meetingId: this.meetingId,
			epoch: this.epoch,
			sequence: this.sequence,
			capturedAt: (/* @__PURE__ */ new Date()).toISOString(),
			sampleStart: this.sampleStart,
			sampleCount: t,
			sampleRate: 16e3,
			channels: 1,
			encoding: "pcm_s16le",
			sha256: await N(e),
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
}(), ae = (e) => P.subscribe(e), oe = () => P.snapshot(), F = "/vendor/mermaid.min.js", I = "script,iframe,frame,object,embed,applet,link,meta,base,form,input,button", L = /* @__PURE__ */ new Set([
	"action",
	"data",
	"formaction",
	"href",
	"poster",
	"src",
	"srcset",
	"xlink:href"
]), R = /(?:@import|expression\s*\(|url\s*\(|javascript:)/i, z = "\n  html { color-scheme: light; }\n  body { margin: 0; padding: 16px; font: 14px system-ui; background: #fff !important; color: #1f2328 !important; }\n  table { border-collapse: collapse; }\n  td, th { border: 1px solid #888; padding: 6px; }\n  pre { white-space: pre-wrap; }\n  .mermaid { display: flex; justify-content: center; background: #fff; color: #1f2328; border: 1px solid #d0d7de; border-radius: 8px; padding: 16px; margin: 12px 0; overflow: auto; }\n  .mermaid svg { width: 100%; max-width: none !important; height: auto; max-height: 85vh; }\n", B = "(function(){\n  var m = window.mermaid;\n  if (!m) return;\n  var KEYWORD = /^(?:flowchart|graph\\s+(?:TD|TB|BT|RL|LR)|sequenceDiagram|classDiagram|stateDiagram|erDiagram|journey|gantt|pie|quadrantChart|requirementDiagram|gitGraph|C4Context|C4Container|C4Component|C4Dynamic|C4Deployment|mindmap|timeline|zenuml|sankey|xychart|block|packet|kanban|architecture|radar|treemap)\\b/;\n  function sourceText(node){\n    var out = '';\n    for (var child = node.firstChild; child; child = child.nextSibling){\n      if (child.nodeType === 3) out += child.nodeValue;\n      else if (child.nodeType === 1) out += child.tagName === 'BR' ? '<br/>' : sourceText(child);\n    }\n    return out;\n  }\n  function copyInto(target, node){\n    for (var child = node.firstChild; child; child = child.nextSibling){\n      if (child.nodeType === 3) target.appendChild(document.createTextNode(child.nodeValue));\n      else if (child.nodeType === 1){\n        if (child.tagName === 'BR') target.appendChild(document.createElement('br'));\n        else copyInto(target, child);\n      }\n    }\n  }\n  function diagramHead(text){\n    // Skip Mermaid front matter (--- ... ---) and %% directive or comment lines,\n    // so the keyword test sees the diagram type itself. Plain YAML or a comment\n    // block that never reaches a diagram keyword is left as ordinary code.\n    return text.replace(/^\\s*---[\\s\\S]*?\\n---[ \\t]*\\n/, '').replace(/^(?:[ \\t]*%%.*(?:\\n|$))+/, '').replace(/^\\s+/, '');\n  }\n  function explicit(code){\n    var parent = code.parentElement;\n    return code.classList.contains('language-mermaid') || code.classList.contains('mermaid')\n      || (parent !== null && parent.tagName === 'PRE' && parent.classList.contains('mermaid'));\n  }\n  function promote(){\n    var nodes = document.querySelectorAll('code.language-mermaid, code.mermaid, pre > code');\n    for (var i = 0; i < nodes.length; i++){\n      var code = nodes[i];\n      if (!code.parentNode) continue;\n      if (!explicit(code) && !KEYWORD.test(diagramHead(sourceText(code)))) continue;\n      var host = code.closest('pre') || code;\n      var div = document.createElement('div');\n      div.className = 'mermaid';\n      copyInto(div, code);\n      if (host.parentNode) host.parentNode.replaceChild(div, host);\n    }\n  }\n  function start(){\n    try { promote(); } catch (e) {}\n    try { m.initialize({startOnLoad:false,securityLevel:'strict',suppressErrorRendering:true,theme:'default'}); } catch (e) {}\n    try { m.run({querySelector:'.mermaid',suppressErrors:true}); } catch (e) {}\n  }\n  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);\n  else start();\n})();";
function V(e) {
	return [
		"default-src 'none'",
		`script-src 'unsafe-inline' ${`${e}${F}`}`,
		"style-src 'unsafe-inline'",
		"img-src data:",
		"font-src data:",
		"connect-src 'none'",
		"form-action 'none'",
		"base-uri 'none'"
	].join("; ");
}
function H() {
	return [
		"default-src 'none'",
		"script-src 'unsafe-inline'",
		"style-src 'unsafe-inline'",
		"img-src data:",
		"font-src data:",
		"connect-src 'none'",
		"form-action 'none'",
		"base-uri 'none'"
	].join("; ");
}
function U(e) {
	e.querySelectorAll(I).forEach((e) => e.remove()), e.querySelectorAll("*").forEach((e) => {
		for (let t of [...e.attributes]) {
			let n = t.name.toLowerCase();
			(n.startsWith("on") || L.has(n) || n === "style" && R.test(t.value)) && e.removeAttribute(t.name);
		}
	}), e.querySelectorAll("style").forEach((e) => {
		R.test(e.textContent ?? "") && e.remove();
	});
}
function se(e, t = window.location.origin) {
	let n = new DOMParser().parseFromString(e, "text/html"), r = document.implementation.createHTMLDocument(""), i = r.createDocumentFragment();
	for (let e of [...n.body.childNodes]) i.appendChild(r.importNode(e, !0));
	U(i);
	let a = r.createElement("meta");
	a.httpEquiv = "Content-Security-Policy", a.content = V(t), r.head.prepend(a);
	let o = r.createElement("meta");
	o.setAttribute("charset", "utf-8"), r.head.appendChild(o);
	let s = r.createElement("style");
	s.textContent = z, r.head.appendChild(s);
	let c = r.createElement("script");
	c.src = `${t}${F}`, c.crossOrigin = "anonymous", r.head.appendChild(c), r.body.appendChild(i);
	let l = r.createElement("script");
	return l.textContent = B, r.body.appendChild(l), `<!doctype html>\n${r.documentElement.outerHTML}`;
}
function ce(e, t) {
	if (/<\/script/i.test(t)) throw Error("The installed Mermaid runtime cannot be embedded safely.");
	let n = new DOMParser().parseFromString(e, "text/html"), r = document.implementation.createHTMLDocument("Meeting sketch"), i = r.createDocumentFragment();
	for (let e of [...n.body.childNodes]) i.appendChild(r.importNode(e, !0));
	U(i);
	let a = r.createElement("meta");
	a.httpEquiv = "Content-Security-Policy", a.content = H(), r.head.prepend(a);
	let o = r.createElement("meta");
	o.setAttribute("charset", "utf-8"), r.head.appendChild(o);
	let s = r.createElement("meta");
	s.name = "viewport", s.content = "width=device-width, initial-scale=1", r.head.appendChild(s);
	let c = r.createElement("style");
	c.textContent = z, r.head.appendChild(c);
	let l = r.createElement("script");
	l.textContent = t, r.head.appendChild(l), r.body.appendChild(i);
	let u = r.createElement("script");
	return u.textContent = B, r.body.appendChild(u), `<!doctype html>\n${r.documentElement.outerHTML}`;
}
//#endregion
//#region src/inputProbe.ts
var W = -120;
function G(e) {
	return e > 0 ? Math.max(W, Math.round(20 * Math.log10(e) * 10) / 10) : W;
}
function K(e) {
	let t = e.reduce((e, t) => Math.max(e, t.channels), 0), n = Array(t).fill(0), r = Array(t).fill(0), i = 0;
	for (let t of e) {
		i += t.frames;
		for (let e = 0; e < t.channels; e++) n[e] += t.sumSquares[e] ?? 0, r[e] = Math.max(r[e], t.peaks[e] ?? 0);
	}
	return {
		channels: t,
		frames: i,
		rmsDb: n.map((e) => G(i > 0 ? Math.sqrt(e / i) : 0)),
		peakDb: r.map(G)
	};
}
function le(e) {
	return e <= W ? "silent" : e < -60 ? "silent (noise floor)" : e < -40 ? "quiet" : "active";
}
var q = [{
	label: "As the app records today",
	requestedChannels: "1",
	audio: {
		channelCount: 1,
		echoCancellation: !1,
		noiseSuppression: !1
	}
}, {
	label: "Asking for every channel",
	requestedChannels: "all",
	audio: {
		channelCount: { ideal: 32 },
		echoCancellation: !1,
		noiseSuppression: !1,
		autoGainControl: !1
	}
}];
async function J(e, t, n) {
	let r = await navigator.mediaDevices.getUserMedia({ audio: e.audio }), i = new AudioContext(), a = [];
	try {
		await i.audioWorklet.addModule(t);
		let o = i.createMediaStreamSource(r), s = new AudioWorkletNode(i, "whisper-meeting-channel-probe");
		s.port.onmessage = (e) => {
			e.data?.type === "levels" && a.push(e.data);
		}, o.connect(s), await new Promise((e) => setTimeout(e, n)), s.port.postMessage({ type: "stop" }), o.disconnect(), s.disconnect();
		let c = r.getAudioTracks()[0], l = c?.getSettings() ?? {};
		return {
			label: e.label,
			requestedChannels: e.requestedChannels,
			trackLabel: c?.label ?? "",
			settings: {
				channelCount: l.channelCount,
				sampleRate: l.sampleRate,
				echoCancellation: l.echoCancellation,
				noiseSuppression: l.noiseSuppression,
				autoGainControl: l.autoGainControl
			},
			levels: K(a)
		};
	} finally {
		r.getTracks().forEach((e) => e.stop()), await i.close().catch(() => void 0);
	}
}
async function ue(e, t = 2500) {
	let n = [];
	for (let r of q) n.push(await J(r, e, t));
	return {
		inputs: (await navigator.mediaDevices.enumerateDevices().catch(() => [])).filter((e) => e.kind === "audioinput").map((e) => e.label || "(unnamed input)"),
		passes: n
	};
}
//#endregion
//#region src/transcript.ts
var Y = 16e3, X = 30 * Y, Z = 2 * Y, Q = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]$/u, de = /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;
function fe(e) {
	let t = Math.floor(e / Y), n = Math.floor(t / 60);
	return `${String(n).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}
function pe(e, t) {
	let n = e.trimEnd(), r = t.trimStart();
	return n ? r ? `${n}${Q.test(n) && de.test(r) ? "" : " "}${r}` : n : r;
}
function me(e) {
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
		let e = (n.get(t.epoch) ?? 0) + t.audio_start_sample, r = Math.floor(e / X);
		a !== void 0 && a.epoch === t.epoch && a.bucket === r && t.audio_start_sample - a.lastEnd <= Z && a ? (a.text = pe(a.text, t.text), a.lastEnd = Math.max(a.lastEnd, t.audio_end_sample), o = r) : (s(), a = {
			key: String(t.id),
			text: t.text.trim(),
			timestamp: r === o ? void 0 : fe(e),
			epoch: t.epoch,
			bucket: r,
			lastEnd: t.audio_end_sample
		}, o = r);
	}
	return s(), i;
}
//#endregion
//#region src/App.tsx
var $ = "/apps/whisper-meeting/api", he = "/apps/whisper-meeting/ui/dist/whisper-meeting-worklet.js";
function ge() {
	let _ = t(), v = i(), [y, b] = p(""), [x, S] = p(), [C, w] = p(""), [T, E] = p(), [D, O] = p(), [k, A] = p("notes"), [j, M] = p({ running: !1 }), [N, F] = p(""), I = ne(ae, oe);
	f(() => {
		P.attach(_);
	}, [_]), f(() => {
		let e = (e) => {
			(I.phase === "recording" || I.queuedChunks > 0) && e.preventDefault();
		};
		return window.addEventListener("beforeunload", e), () => window.removeEventListener("beforeunload", e);
	}, [I.phase, I.queuedChunks]);
	let L = r({
		queryKey: ["meetings"],
		queryFn: () => _.get(`${$}/meetings`)
	}), R = r({
		queryKey: ["whisper-meeting-model"],
		queryFn: () => _.get(`${$}/model`),
		refetchInterval: (e) => e.state.data?.model.status === "installing" && 1e3
	}), z = r({
		queryKey: ["meeting", x],
		queryFn: () => _.get(`${$}/meetings/${encodeURIComponent(x ?? "")}`),
		enabled: !!x
	}), B = r({
		queryKey: ["meeting-transcript", x],
		queryFn: () => _.get(`${$}/meetings/${encodeURIComponent(x ?? "")}/transcript`),
		enabled: !!x
	}), V = r({
		queryKey: ["meeting-agents", x],
		queryFn: () => _.get(`${$}/meetings/${encodeURIComponent(x ?? "")}/agents`),
		enabled: !!x,
		refetchInterval: (e) => re(e.state.data?.agents ?? []) ? 1e3 : !1
	}), H = n({
		mutationFn: () => _.post(`${$}/model/install`, {}),
		onSuccess: async () => {
			await v.invalidateQueries({ queryKey: ["whisper-meeting-model"] });
		}
	}), U = n({
		mutationFn: (e) => _.post(`${$}/meetings`, { title: e }),
		onSuccess: async () => {
			b(""), await v.invalidateQueries({ queryKey: ["meetings"] });
		}
	}), W = n({
		mutationFn: (e) => _.post(`${$}/meetings/${encodeURIComponent(x ?? "")}/context`, { text: e }),
		onSuccess: async () => {
			F(""), await v.invalidateQueries({ queryKey: ["meeting-agents", x] });
		}
	});
	f(() => {
		if (!x) return;
		let e = new EventSource(`${$}/meetings/${encodeURIComponent(x)}/events`);
		return e.onmessage = () => {
			v.invalidateQueries({ queryKey: ["meetings"] }), v.invalidateQueries({ queryKey: ["meeting", x] }), v.invalidateQueries({ queryKey: ["meeting-transcript", x] }), v.invalidateQueries({ queryKey: ["meeting-agents", x] });
		}, () => e.close();
	}, [v, x]), f(() => {
		z.data && P.markServerState(z.data.meeting.id, z.data.meeting.state);
	}, [z.data]);
	async function G(e, t) {
		S(e), w("");
		try {
			await t(), await Promise.all([
				v.invalidateQueries({ queryKey: ["meetings"] }),
				v.invalidateQueries({ queryKey: ["meeting", e] }),
				v.invalidateQueries({ queryKey: ["meeting-transcript", e] }),
				v.invalidateQueries({ queryKey: ["meeting-agents", e] })
			]);
		} catch (e) {
			w(e instanceof Error ? e.message : String(e));
		}
	}
	async function K(e, t) {
		w("");
		try {
			let n = await _.raw(`${$}/meetings/${encodeURIComponent(e)}/export?format=${t}`), r = (n.headers.get("Content-Disposition") ?? "").match(/filename="([^"]+)"/)?.[1] ?? `meeting.${t === "text" ? "txt" : t === "markdown" ? "md" : "json"}`, i = await n.blob();
			if (t === "sketch") {
				let e = await fetch("/vendor/mermaid.min.js");
				if (!e.ok) throw Error(`Could not load the local Mermaid runtime (${e.status}).`);
				let t = ce(await i.text(), await e.text());
				i = new Blob([t], { type: "text/html;charset=utf-8" });
			}
			let a = URL.createObjectURL(i), o = document.createElement("a");
			o.href = a, o.download = r, o.click(), URL.revokeObjectURL(a);
		} catch (e) {
			w(e instanceof Error ? e.message : String(e));
		}
	}
	async function q(e) {
		let t = e.title || "Untitled meeting";
		if (window.confirm(`Permanently delete “${t}” and its local data?`)) {
			w("");
			try {
				await _.del(`${$}/meetings/${encodeURIComponent(e.id)}`), x === e.id && S(void 0), await v.invalidateQueries({ queryKey: ["meetings"] });
			} catch (e) {
				w(e instanceof Error ? e.message : String(e));
			}
		}
	}
	async function J(e) {
		if (x) {
			w(""), E(e);
			try {
				await _.post(`${$}/meetings/${encodeURIComponent(x)}/segments/${e}/retry`, {}), await Promise.all([v.invalidateQueries({ queryKey: ["meeting", x] }), v.invalidateQueries({ queryKey: ["meeting-transcript", x] })]);
			} catch (e) {
				w(e instanceof Error ? e.message : String(e));
			} finally {
				E(void 0);
			}
		}
	}
	async function Y(e) {
		if (x) {
			w(""), O(e);
			try {
				await _.post(`${$}/meetings/${encodeURIComponent(x)}/agents/${encodeURIComponent(e)}/retry`, {}), await v.invalidateQueries({ queryKey: ["meeting-agents", x] });
			} catch (e) {
				w(e instanceof Error ? e.message : String(e));
			} finally {
				O(void 0);
			}
		}
	}
	let X = L.data?.meetings ?? [], Z = X.find((e) => e.last_error), Q = L.error ?? R.error ?? U.error ?? H.error ?? z.error ?? B.error ?? V.error ?? W.error, de = Q instanceof Error ? Q.message : String(Q ?? "");
	return /* @__PURE__ */ g(m, { children: [/* @__PURE__ */ h(te, {
		title: "Whisper Meeting",
		subtitle: "Durable, local-first meeting transcription"
	}), /* @__PURE__ */ g("div", {
		className: "px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0",
		children: [
			/* @__PURE__ */ g("div", {
				className: "grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6",
				children: [
					/* @__PURE__ */ h(d, {
						label: "Meetings",
						value: String(X.length),
						accent: !0
					}),
					/* @__PURE__ */ h(d, {
						label: "Storage",
						value: "Local"
					}),
					/* @__PURE__ */ h(d, {
						label: "Whisper model",
						value: R.data?.model.status ?? "Checking"
					}),
					/* @__PURE__ */ h(d, {
						label: "Capture",
						value: I.phase
					}),
					/* @__PURE__ */ h(d, {
						label: "Upload queue",
						value: String(I.queuedChunks)
					})
				]
			}),
			Q ? /* @__PURE__ */ h("div", {
				className: "mb-4",
				children: /* @__PURE__ */ h(l, {
					title: "Whisper Meeting could not load",
					message: de
				})
			}) : null,
			I.fatalError || I.uploadError ? /* @__PURE__ */ h("div", {
				className: "mb-4",
				children: /* @__PURE__ */ h(l, {
					title: I.fatalError ? "Capture stopped" : "Upload will retry",
					message: I.fatalError ?? I.uploadError
				})
			}) : null,
			Z ? /* @__PURE__ */ h("div", {
				className: "mb-4",
				children: /* @__PURE__ */ h(l, {
					title: "Recognition is waiting to recover",
					message: `${Z.title || "Untitled meeting"}: ${Z.last_error}`
				})
			}) : null,
			C ? /* @__PURE__ */ h("div", {
				className: "mb-4",
				children: /* @__PURE__ */ h(l, {
					title: "Meeting action failed",
					message: C
				})
			}) : null,
			I.phase === "recording" ? /* @__PURE__ */ g(o, {
				className: "mb-4",
				children: [
					/* @__PURE__ */ h(s, { children: "Recording locally" }),
					/* @__PURE__ */ h("p", {
						className: "text-sm text-muted mt-2",
						children: "Audio is committed to IndexedDB before upload. Closing this page does not stop the module-level capture engine."
					}),
					/* @__PURE__ */ g("div", {
						className: "mt-3 flex gap-2",
						children: [/* @__PURE__ */ h(a, {
							onClick: () => {
								I.meetingId && G(I.meetingId, () => P.pause(_));
							},
							children: "Pause"
						}), /* @__PURE__ */ h(a, {
							onClick: () => {
								I.meetingId && G(I.meetingId, () => P.end(_));
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
			].includes(I.phase) ? /* @__PURE__ */ g(o, {
				className: "mb-4",
				children: [/* @__PURE__ */ h(s, { children: "Finishing the durable cut" }), /* @__PURE__ */ h("p", {
					className: "text-sm text-muted mt-2",
					children: I.phase === "transcribing" ? "All captured audio is durable. Waiting for final transcript coverage." : "Keeping the meeting open until every local chunk has a durable backend ACK."
				})]
			}) : null,
			R.data?.model.status === "ready" ? null : /* @__PURE__ */ g(o, {
				className: "mb-4",
				children: [
					/* @__PURE__ */ h(s, { children: "Local Whisper model" }),
					/* @__PURE__ */ h("p", {
						className: "text-sm text-muted mt-2",
						children: "Whisper Meeting keeps its own verified large-v3-turbo model so Kiro Crew updates cannot replace it."
					}),
					R.data?.model.error ? /* @__PURE__ */ h("p", {
						className: "text-sm text-danger mt-2",
						children: R.data.model.error
					}) : null,
					/* @__PURE__ */ h("div", {
						className: "mt-3",
						children: /* @__PURE__ */ h(a, {
							onClick: () => H.mutate(),
							disabled: R.data?.model.status === "installing" || H.isPending,
							children: R.data?.model.status === "installing" ? "Installing and testing…" : "Install local model"
						})
					})
				]
			}),
			I.phase === "recording" ? null : /* @__PURE__ */ g(o, {
				className: "mb-4",
				children: [
					/* @__PURE__ */ h(s, { children: "Input check" }),
					/* @__PURE__ */ h("p", {
						className: "text-sm text-muted mt-2",
						children: "Opens the default input for about five seconds, once as the app records today and once asking for every channel, and shows the level on each channel. Speak, and let the call play, while it runs. On an aggregate device the microphone is channel 1 and the call is channels 2 and 3."
					}),
					/* @__PURE__ */ h("div", {
						className: "mt-3 flex gap-2 items-center",
						children: /* @__PURE__ */ h(a, {
							disabled: j.running || [
								"starting",
								"stopping",
								"uploading"
							].includes(I.phase),
							onClick: () => {
								M({ running: !0 }), ue(he).then((e) => M({
									running: !1,
									report: e
								})).catch((e) => M({
									running: !1,
									error: e instanceof Error ? e.message : String(e)
								}));
							},
							children: j.running ? "Listening…" : "Check input device"
						})
					}),
					j.error ? /* @__PURE__ */ h("p", {
						className: "text-sm text-danger mt-2",
						children: j.error
					}) : null,
					j.report ? /* @__PURE__ */ g("div", {
						className: "mt-3 text-sm",
						children: [/* @__PURE__ */ g("p", {
							className: "text-muted",
							children: ["Audio inputs visible to the browser: ", j.report.inputs.join(", ") || "none"]
						}), j.report.passes.map((e) => /* @__PURE__ */ g("div", {
							className: "mt-3",
							children: [
								/* @__PURE__ */ g("p", {
									className: "font-medium text-text",
									children: [
										e.label,
										" (requested channels: ",
										e.requestedChannels,
										")"
									]
								}),
								/* @__PURE__ */ g("p", {
									className: "text-muted",
									children: [
										"Device: ",
										e.trackLabel || "unknown",
										".",
										" ",
										"Track settings: ",
										e.settings.channelCount ?? "?",
										" ch, ",
										e.settings.sampleRate ?? "?",
										" Hz,",
										" ",
										"echo cancellation ",
										String(e.settings.echoCancellation ?? "?"),
										",",
										" ",
										"noise suppression ",
										String(e.settings.noiseSuppression ?? "?"),
										",",
										" ",
										"auto gain ",
										String(e.settings.autoGainControl ?? "?"),
										"."
									]
								}),
								/* @__PURE__ */ g("p", {
									className: "text-text",
									children: [
										"Delivered to the worklet: ",
										/* @__PURE__ */ h("strong", { children: e.levels.channels }),
										" channel",
										e.levels.channels === 1 ? "" : "s"
									]
								}),
								/* @__PURE__ */ h("ul", {
									className: "list-disc ml-5",
									children: e.levels.rmsDb.map((t, n) => /* @__PURE__ */ g("li", { children: [
										"Channel ",
										n + 1,
										": RMS ",
										t,
										" dBFS, peak ",
										e.levels.peakDb[n],
										" dBFS (",
										le(t),
										")"
									] }, n))
								})
							]
						}, e.label))]
					}) : null
				]
			}),
			/* @__PURE__ */ g(o, {
				className: "mb-4",
				children: [/* @__PURE__ */ h(s, { children: "New meeting" }), /* @__PURE__ */ g("div", {
					className: "flex flex-col md:flex-row gap-3 mt-3",
					children: [/* @__PURE__ */ h(u, {
						value: y,
						onChange: (e) => b(e.target.value),
						placeholder: "Optional meeting title",
						"aria-label": "Meeting title",
						maxLength: 200
					}), /* @__PURE__ */ h(a, {
						onClick: () => U.mutate(y),
						disabled: U.isPending,
						children: U.isPending ? "Creating…" : "Create meeting"
					})]
				})]
			}),
			/* @__PURE__ */ g(o, { children: [/* @__PURE__ */ h(s, { children: "Meeting history" }), L.isPending ? /* @__PURE__ */ h("p", {
				className: "text-sm text-muted mt-3",
				children: "Loading meetings…"
			}) : X.length === 0 ? /* @__PURE__ */ h(c, {
				icon: /* @__PURE__ */ h("span", {
					"aria-hidden": "true",
					children: "🎙️"
				}),
				title: "No meetings yet",
				subtitle: "Create a meeting to verify the durable app store."
			}) : /* @__PURE__ */ h("div", {
				className: "divide-y divide-border mt-3",
				children: X.map((t) => /* @__PURE__ */ g("div", {
					className: "py-3 first:pt-0 last:pb-0",
					children: [/* @__PURE__ */ g("div", {
						className: "flex items-center justify-between gap-3",
						children: [/* @__PURE__ */ h("div", {
							className: "font-medium text-text",
							children: t.title || "Untitled meeting"
						}), /* @__PURE__ */ g("div", {
							className: "flex gap-2",
							children: [
								/* @__PURE__ */ h(a, {
									onClick: () => S(t.id),
									children: "View"
								}),
								t.state === "draft" && I.phase !== "recording" ? /* @__PURE__ */ h(a, {
									onClick: () => void G(t.id, () => P.start(_, t.id)),
									disabled: I.phase === "starting" || R.data?.model.status !== "ready",
									children: "Record"
								}) : null,
								t.state === "paused" && I.phase !== "recording" ? /* @__PURE__ */ g(m, { children: [/* @__PURE__ */ h(a, {
									onClick: () => void G(t.id, () => P.start(_, t.id, !0)),
									disabled: I.phase === "starting" || R.data?.model.status !== "ready",
									children: "Resume"
								}), /* @__PURE__ */ h(a, {
									onClick: () => void G(t.id, () => P.recover(_, t.id, "end")),
									children: "End"
								})] }) : null,
								t.state === "recording" && I.meetingId !== t.id ? /* @__PURE__ */ h(a, {
									onClick: () => void G(t.id, () => P.recover(_, t.id, "pause")),
									children: "Recover & pause"
								}) : null,
								["recording", "draining"].includes(t.state) ? null : /* @__PURE__ */ h(a, {
									onClick: () => void q(t),
									children: "Delete"
								})
							]
						})]
					}), /* @__PURE__ */ g("div", {
						className: "text-sm text-muted mt-1",
						children: [
							t.state,
							" · ",
							e(t.created_at)
						]
					})]
				}, t.id))
			})] }),
			x ? /* @__PURE__ */ g(o, {
				className: "mt-4",
				children: [
					/* @__PURE__ */ g("div", {
						className: "flex flex-wrap items-start justify-between gap-3",
						children: [/* @__PURE__ */ g("div", { children: [/* @__PURE__ */ h(s, { children: "Meeting workspace" }), z.data ? /* @__PURE__ */ g("p", {
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
						}) : null] }), /* @__PURE__ */ h("div", {
							className: "flex flex-wrap gap-2 text-xs text-muted",
							children: (V.data?.agents ?? []).map((e) => /* @__PURE__ */ g("span", {
								className: "rounded-full border border-border px-2 py-1",
								children: [
									e.agent_id === "whisper-meeting-note-taker" ? "Note Taker" : "Sketch Artist",
									":",
									" ",
									ie(e, z.data?.meeting.state),
									(() => {
										let t = (V.data?.dispatch_counts ?? []).filter((t) => t.agent_id === e.agent_id && t.state === "pending").reduce((e, t) => e + t.count, 0);
										return t ? ` · ${t} queued` : "";
									})()
								]
							}, e.agent_id))
						})]
					}),
					(V.data?.agents ?? []).filter((e) => e.state === "failed").map((e) => /* @__PURE__ */ g("div", {
						className: "mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-danger/40 px-3 py-2",
						children: [/* @__PURE__ */ g("p", {
							className: "text-sm text-danger",
							children: [
								e.agent_id === "whisper-meeting-note-taker" ? "Note Taker" : "Sketch Artist",
								" failed",
								e.last_error ? `: ${e.last_error}` : ".",
								" ",
								"Transcription is unaffected."
							]
						}), /* @__PURE__ */ h(a, {
							onClick: () => void Y(e.agent_id),
							disabled: D !== void 0,
							children: D === e.agent_id ? "Retrying…" : "Retry agent"
						})]
					}, `${e.agent_id}-failure`)),
					z.data?.meeting.last_error ? /* @__PURE__ */ g("p", {
						className: "text-sm text-danger mt-2",
						children: ["Recognition will retry without deleting audio: ", z.data.meeting.last_error]
					}) : null,
					/* @__PURE__ */ g("div", {
						className: "mt-4 grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.9fr)]",
						children: [/* @__PURE__ */ g("section", {
							className: "min-w-0",
							children: [/* @__PURE__ */ g("div", {
								className: "flex gap-2 border-b border-border pb-2",
								children: [
									/* @__PURE__ */ h(a, {
										onClick: () => A("notes"),
										disabled: k === "notes",
										children: "Notes"
									}),
									/* @__PURE__ */ h(a, {
										onClick: () => A("sketch"),
										disabled: k === "sketch",
										children: "Sketch"
									}),
									/* @__PURE__ */ h(a, {
										onClick: () => void K(x, "notes"),
										children: "Export notes"
									}),
									/* @__PURE__ */ h(a, {
										onClick: () => void K(x, "sketch"),
										children: "Export sketch"
									}),
									/* @__PURE__ */ h(a, {
										onClick: () => void K(x, "combined"),
										children: "Combined"
									})
								]
							}), /* @__PURE__ */ h("div", {
								className: "mt-3 min-h-64 rounded-md border border-border p-4",
								children: k === "notes" ? /* @__PURE__ */ h(ee, { content: V.data?.outputs["whisper-meeting-note-taker"] ?? "_Notes pending._" }) : /* @__PURE__ */ g("div", { children: [/* @__PURE__ */ h("p", {
									className: "text-xs text-muted mb-3",
									children: "Sketch output is isolated with scripts, navigation, and network access disabled."
								}), /* @__PURE__ */ h("iframe", {
									title: "Meeting sketch",
									sandbox: "allow-scripts",
									srcDoc: se(V.data?.outputs["whisper-meeting-sketch-artist"] ?? "<p>Sketch pending.</p>"),
									className: "w-full rounded-md border border-border bg-surface",
									style: { height: "clamp(36rem, 70vh, 52rem)" }
								})] })
							})]
						}), /* @__PURE__ */ g("section", {
							className: "min-w-0 lg:border-l lg:border-border lg:pl-5",
							children: [/* @__PURE__ */ g("div", {
								className: "flex flex-wrap items-center justify-between gap-2",
								children: [/* @__PURE__ */ h("h3", {
									className: "font-medium text-text",
									children: "Transcript"
								}), /* @__PURE__ */ g("div", {
									className: "flex flex-wrap gap-2",
									children: [
										/* @__PURE__ */ h(a, {
											onClick: () => void K(x, "markdown"),
											children: "Markdown"
										}),
										/* @__PURE__ */ h(a, {
											onClick: () => void K(x, "text"),
											children: "Text"
										}),
										/* @__PURE__ */ h(a, {
											onClick: () => void K(x, "json"),
											children: "JSON"
										})
									]
								})]
							}), /* @__PURE__ */ g("div", {
								className: "mt-3 max-h-[32rem] space-y-2 overflow-y-auto pr-2",
								children: [me(B.data?.segments ?? []).map((e) => e.kind === "speech" ? /* @__PURE__ */ g("div", { children: [e.timestamp ? /* @__PURE__ */ h("div", {
									className: "mb-1 mt-4 text-xs font-medium tabular-nums text-muted",
									children: e.timestamp
								}) : null, /* @__PURE__ */ h("p", {
									className: "text-sm leading-relaxed text-text",
									children: e.text
								})] }, e.key) : /* @__PURE__ */ g("div", {
									className: "flex items-center justify-between gap-3 rounded-md bg-muted/20 px-3 py-2",
									children: [/* @__PURE__ */ g("span", {
										className: "text-xs text-muted",
										children: [
											"Silence covered",
											" · ",
											(e.segment.audio_start_sample / 16e3).toFixed(1),
											"–",
											(e.segment.audio_end_sample / 16e3).toFixed(1),
											"s"
										]
									}), /* @__PURE__ */ h(a, {
										onClick: () => void J(e.segment.id),
										disabled: T !== void 0,
										children: T === e.segment.id ? "Retrying…" : "Recognize again"
									})]
								}, e.key)), B.data && B.data.segments.length === 0 ? /* @__PURE__ */ h("p", {
									className: "text-sm text-muted",
									children: "No committed speech yet."
								}) : null]
							})]
						})]
					}),
					/* @__PURE__ */ g("section", {
						className: "mt-5 border-t border-border pt-4",
						children: [
							/* @__PURE__ */ h("h3", {
								className: "font-medium text-text",
								children: "Meeting Chat"
							}),
							/* @__PURE__ */ h("p", {
								className: "mt-1 text-xs text-muted",
								children: "Add participants, agenda, terminology, or corrections. Messages go to both agents and never rewrite the raw transcript."
							}),
							(V.data?.context ?? []).length > 0 ? /* @__PURE__ */ h("div", {
								className: "mt-3 max-h-32 space-y-2 overflow-y-auto",
								children: V.data?.context.map((t) => /* @__PURE__ */ g("div", {
									className: "rounded-md bg-muted/20 px-3 py-2 text-sm",
									children: [/* @__PURE__ */ g("span", {
										className: "mr-2 text-xs text-muted",
										children: ["Typed · ", e(t.created_at)]
									}), t.text]
								}, t.id))
							}) : null,
							/* @__PURE__ */ g("form", {
								className: "mt-3 flex flex-col gap-2 md:flex-row",
								onSubmit: (e) => {
									e.preventDefault();
									let t = N.trim();
									z.data?.meeting.state === "recording" && t && !W.isPending && W.mutate(t);
								},
								children: [/* @__PURE__ */ h(u, {
									value: N,
									onChange: (e) => F(e.target.value),
									placeholder: "Tell the meeting participants, agenda, or a correction…",
									"aria-label": "Meeting context",
									maxLength: 4e3,
									disabled: z.data?.meeting.state !== "recording"
								}), /* @__PURE__ */ h(a, {
									type: "submit",
									disabled: z.data?.meeting.state !== "recording" || !N.trim() || W.isPending,
									children: W.isPending ? "Sending…" : "Send"
								})]
							}),
							z.data?.meeting.state === "recording" ? null : /* @__PURE__ */ h("p", {
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
export { ge as default };
