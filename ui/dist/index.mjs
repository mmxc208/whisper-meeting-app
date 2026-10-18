import { fmtDateTime as e, useAppApi as t } from "@kirocrew/app-sdk";
import { useMutation as n, useQuery as r, useQueryClient as i } from "@tanstack/react-query";
import { Btn as a, Card as o, CardTitle as s, EmptyState as c, ErrorNotice as l, Input as u, PageHeader as d, StatCard as f } from "@kirocrew/app-sdk/ui";
import { useEffect as p, useState as m, useSyncExternalStore as h } from "react";
import { Fragment as g, jsx as _, jsxs as v } from "react/jsx-runtime";
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
}(), L = (e) => I.subscribe(e), R = () => I.snapshot(), z = 16e3, B = 30 * z, V = 2 * z, H = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]$/u, U = /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;
function W(e) {
	let t = Math.floor(e / z), n = Math.floor(t / 60);
	return `${String(n).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}
function G(e, t) {
	let n = e.trimEnd(), r = t.trimStart();
	return n ? r ? `${n}${H.test(n) && U.test(r) ? "" : " "}${r}` : n : r;
}
function K(e) {
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
		let e = (n.get(t.epoch) ?? 0) + t.audio_start_sample, r = Math.floor(e / B);
		a !== void 0 && a.epoch === t.epoch && a.bucket === r && t.audio_start_sample - a.lastEnd <= V && a ? (a.text = G(a.text, t.text), a.lastEnd = Math.max(a.lastEnd, t.audio_end_sample), o = r) : (s(), a = {
			key: String(t.id),
			text: t.text.trim(),
			timestamp: r === o ? void 0 : W(e),
			epoch: t.epoch,
			bucket: r,
			lastEnd: t.audio_end_sample
		}, o = r);
	}
	return s(), i;
}
//#endregion
//#region src/App.tsx
var q = "/apps/whisper-meeting/api";
function J() {
	let y = t(), b = i(), [x, S] = m(""), [C, w] = m(), [T, E] = m(""), [D, O] = m(), k = h(L, R);
	p(() => {
		I.attach(y);
	}, [y]), p(() => {
		let e = (e) => {
			(k.phase === "recording" || k.queuedChunks > 0) && e.preventDefault();
		};
		return window.addEventListener("beforeunload", e), () => window.removeEventListener("beforeunload", e);
	}, [k.phase, k.queuedChunks]);
	let A = r({
		queryKey: ["meetings"],
		queryFn: () => y.get(`${q}/meetings`)
	}), j = r({
		queryKey: ["whisper-meeting-model"],
		queryFn: () => y.get(`${q}/model`),
		refetchInterval: (e) => e.state.data?.model.status === "installing" && 1e3
	}), M = r({
		queryKey: ["meeting", C],
		queryFn: () => y.get(`${q}/meetings/${encodeURIComponent(C ?? "")}`),
		enabled: !!C
	}), N = r({
		queryKey: ["meeting-transcript", C],
		queryFn: () => y.get(`${q}/meetings/${encodeURIComponent(C ?? "")}/transcript`),
		enabled: !!C
	}), P = n({
		mutationFn: () => y.post(`${q}/model/install`, {}),
		onSuccess: async () => {
			await b.invalidateQueries({ queryKey: ["whisper-meeting-model"] });
		}
	}), F = n({
		mutationFn: (e) => y.post(`${q}/meetings`, { title: e }),
		onSuccess: async () => {
			S(""), await b.invalidateQueries({ queryKey: ["meetings"] });
		}
	});
	p(() => {
		if (!C) return;
		let e = new EventSource(`${q}/meetings/${encodeURIComponent(C)}/events`);
		return e.onmessage = () => {
			b.invalidateQueries({ queryKey: ["meetings"] }), b.invalidateQueries({ queryKey: ["meeting", C] }), b.invalidateQueries({ queryKey: ["meeting-transcript", C] });
		}, () => e.close();
	}, [b, C]), p(() => {
		M.data && I.markServerState(M.data.meeting.id, M.data.meeting.state);
	}, [M.data]);
	async function z(e, t) {
		w(e), E("");
		try {
			await t(), await Promise.all([
				b.invalidateQueries({ queryKey: ["meetings"] }),
				b.invalidateQueries({ queryKey: ["meeting", e] }),
				b.invalidateQueries({ queryKey: ["meeting-transcript", e] })
			]);
		} catch (e) {
			E(e instanceof Error ? e.message : String(e));
		}
	}
	async function B(e, t) {
		E("");
		try {
			let n = await y.raw(`${q}/meetings/${encodeURIComponent(e)}/export?format=${t}`), r = (n.headers.get("Content-Disposition") ?? "").match(/filename="([^"]+)"/)?.[1] ?? `meeting.${t === "text" ? "txt" : t === "markdown" ? "md" : "json"}`, i = URL.createObjectURL(await n.blob()), a = document.createElement("a");
			a.href = i, a.download = r, a.click(), URL.revokeObjectURL(i);
		} catch (e) {
			E(e instanceof Error ? e.message : String(e));
		}
	}
	async function V(e) {
		let t = e.title || "Untitled meeting";
		if (window.confirm(`Permanently delete “${t}” and its local data?`)) {
			E("");
			try {
				await y.del(`${q}/meetings/${encodeURIComponent(e.id)}`), C === e.id && w(void 0), await b.invalidateQueries({ queryKey: ["meetings"] });
			} catch (e) {
				E(e instanceof Error ? e.message : String(e));
			}
		}
	}
	async function H(e) {
		if (C) {
			E(""), O(e);
			try {
				await y.post(`${q}/meetings/${encodeURIComponent(C)}/segments/${e}/retry`, {}), await Promise.all([b.invalidateQueries({ queryKey: ["meeting", C] }), b.invalidateQueries({ queryKey: ["meeting-transcript", C] })]);
			} catch (e) {
				E(e instanceof Error ? e.message : String(e));
			} finally {
				O(void 0);
			}
		}
	}
	let U = A.data?.meetings ?? [], W = U.find((e) => e.last_error), G = A.error ?? j.error ?? F.error ?? P.error ?? M.error ?? N.error, J = G instanceof Error ? G.message : String(G ?? "");
	return /* @__PURE__ */ v(g, { children: [/* @__PURE__ */ _(d, {
		title: "Whisper Meeting",
		subtitle: "Durable, local-first meeting transcription"
	}), /* @__PURE__ */ v("div", {
		className: "px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0",
		children: [
			/* @__PURE__ */ v("div", {
				className: "grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6",
				children: [
					/* @__PURE__ */ _(f, {
						label: "Meetings",
						value: String(U.length),
						accent: !0
					}),
					/* @__PURE__ */ _(f, {
						label: "Storage",
						value: "Local"
					}),
					/* @__PURE__ */ _(f, {
						label: "Whisper model",
						value: j.data?.model.status ?? "Checking"
					}),
					/* @__PURE__ */ _(f, {
						label: "Capture",
						value: k.phase
					}),
					/* @__PURE__ */ _(f, {
						label: "Upload queue",
						value: String(k.queuedChunks)
					})
				]
			}),
			G ? /* @__PURE__ */ _("div", {
				className: "mb-4",
				children: /* @__PURE__ */ _(l, {
					title: "Whisper Meeting could not load",
					message: J
				})
			}) : null,
			k.fatalError || k.uploadError ? /* @__PURE__ */ _("div", {
				className: "mb-4",
				children: /* @__PURE__ */ _(l, {
					title: k.fatalError ? "Capture stopped" : "Upload will retry",
					message: k.fatalError ?? k.uploadError
				})
			}) : null,
			W ? /* @__PURE__ */ _("div", {
				className: "mb-4",
				children: /* @__PURE__ */ _(l, {
					title: "Recognition is waiting to recover",
					message: `${W.title || "Untitled meeting"}: ${W.last_error}`
				})
			}) : null,
			T ? /* @__PURE__ */ _("div", {
				className: "mb-4",
				children: /* @__PURE__ */ _(l, {
					title: "Meeting action failed",
					message: T
				})
			}) : null,
			k.phase === "recording" ? /* @__PURE__ */ v(o, {
				className: "mb-4",
				children: [
					/* @__PURE__ */ _(s, { children: "Recording locally" }),
					/* @__PURE__ */ _("p", {
						className: "text-sm text-muted mt-2",
						children: "Audio is committed to IndexedDB before upload. Closing this page does not stop the module-level capture engine."
					}),
					/* @__PURE__ */ v("div", {
						className: "mt-3 flex gap-2",
						children: [/* @__PURE__ */ _(a, {
							onClick: () => {
								k.meetingId && z(k.meetingId, () => I.pause(y));
							},
							children: "Pause"
						}), /* @__PURE__ */ _(a, {
							onClick: () => {
								k.meetingId && z(k.meetingId, () => I.end(y));
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
			].includes(k.phase) ? /* @__PURE__ */ v(o, {
				className: "mb-4",
				children: [/* @__PURE__ */ _(s, { children: "Finishing the durable cut" }), /* @__PURE__ */ _("p", {
					className: "text-sm text-muted mt-2",
					children: k.phase === "transcribing" ? "All captured audio is durable. Waiting for final transcript coverage." : "Keeping the meeting open until every local chunk has a durable backend ACK."
				})]
			}) : null,
			j.data?.model.status === "ready" ? null : /* @__PURE__ */ v(o, {
				className: "mb-4",
				children: [
					/* @__PURE__ */ _(s, { children: "Local Whisper model" }),
					/* @__PURE__ */ _("p", {
						className: "text-sm text-muted mt-2",
						children: "Whisper Meeting keeps its own verified large-v3-turbo model so Kiro Crew updates cannot replace it."
					}),
					j.data?.model.error ? /* @__PURE__ */ _("p", {
						className: "text-sm text-danger mt-2",
						children: j.data.model.error
					}) : null,
					/* @__PURE__ */ _("div", {
						className: "mt-3",
						children: /* @__PURE__ */ _(a, {
							onClick: () => P.mutate(),
							disabled: j.data?.model.status === "installing" || P.isPending,
							children: j.data?.model.status === "installing" ? "Installing and testing…" : "Install local model"
						})
					})
				]
			}),
			/* @__PURE__ */ v(o, {
				className: "mb-4",
				children: [/* @__PURE__ */ _(s, { children: "New meeting" }), /* @__PURE__ */ v("div", {
					className: "flex flex-col md:flex-row gap-3 mt-3",
					children: [/* @__PURE__ */ _(u, {
						value: x,
						onChange: (e) => S(e.target.value),
						placeholder: "Optional meeting title",
						"aria-label": "Meeting title",
						maxLength: 200
					}), /* @__PURE__ */ _(a, {
						onClick: () => F.mutate(x),
						disabled: F.isPending,
						children: F.isPending ? "Creating…" : "Create meeting"
					})]
				})]
			}),
			/* @__PURE__ */ v(o, { children: [/* @__PURE__ */ _(s, { children: "Meeting history" }), A.isPending ? /* @__PURE__ */ _("p", {
				className: "text-sm text-muted mt-3",
				children: "Loading meetings…"
			}) : U.length === 0 ? /* @__PURE__ */ _(c, {
				icon: /* @__PURE__ */ _("span", {
					"aria-hidden": "true",
					children: "🎙️"
				}),
				title: "No meetings yet",
				subtitle: "Create a meeting to verify the durable app store."
			}) : /* @__PURE__ */ _("div", {
				className: "divide-y divide-border mt-3",
				children: U.map((t) => /* @__PURE__ */ v("div", {
					className: "py-3 first:pt-0 last:pb-0",
					children: [/* @__PURE__ */ v("div", {
						className: "flex items-center justify-between gap-3",
						children: [/* @__PURE__ */ _("div", {
							className: "font-medium text-text",
							children: t.title || "Untitled meeting"
						}), /* @__PURE__ */ v("div", {
							className: "flex gap-2",
							children: [
								/* @__PURE__ */ _(a, {
									onClick: () => w(t.id),
									children: "View"
								}),
								t.state === "draft" && k.phase !== "recording" ? /* @__PURE__ */ _(a, {
									onClick: () => void z(t.id, () => I.start(y, t.id)),
									disabled: k.phase === "starting" || j.data?.model.status !== "ready",
									children: "Record"
								}) : null,
								t.state === "paused" && k.phase !== "recording" ? /* @__PURE__ */ v(g, { children: [/* @__PURE__ */ _(a, {
									onClick: () => void z(t.id, () => I.start(y, t.id, !0)),
									disabled: k.phase === "starting" || j.data?.model.status !== "ready",
									children: "Resume"
								}), /* @__PURE__ */ _(a, {
									onClick: () => void z(t.id, () => I.recover(y, t.id, "end")),
									children: "End"
								})] }) : null,
								t.state === "recording" && k.meetingId !== t.id ? /* @__PURE__ */ _(a, {
									onClick: () => void z(t.id, () => I.recover(y, t.id, "pause")),
									children: "Recover & pause"
								}) : null,
								["recording", "draining"].includes(t.state) ? null : /* @__PURE__ */ _(a, {
									onClick: () => void V(t),
									children: "Delete"
								})
							]
						})]
					}), /* @__PURE__ */ v("div", {
						className: "text-sm text-muted mt-1",
						children: [
							t.state,
							" · ",
							e(t.created_at)
						]
					})]
				}, t.id))
			})] }),
			C ? /* @__PURE__ */ v(o, {
				className: "mt-4",
				children: [
					/* @__PURE__ */ _(s, { children: "Transcript" }),
					/* @__PURE__ */ v("div", {
						className: "mt-3 flex gap-2",
						children: [
							/* @__PURE__ */ _(a, {
								onClick: () => void B(C, "markdown"),
								children: "Export Markdown"
							}),
							/* @__PURE__ */ _(a, {
								onClick: () => void B(C, "text"),
								children: "Export text"
							}),
							/* @__PURE__ */ _(a, {
								onClick: () => void B(C, "json"),
								children: "Export JSON"
							})
						]
					}),
					M.data ? /* @__PURE__ */ v(g, { children: [/* @__PURE__ */ v("p", {
						className: "text-sm text-muted mt-2",
						children: [
							M.data.meeting.state,
							" · ",
							M.data.progress.transcribed_chunks,
							"/",
							M.data.progress.chunks,
							" chunks covered",
							` · ${M.data.progress.pending_chunks} awaiting recognition`,
							M.data.operation ? ` · ${M.data.operation.kind} ${M.data.operation.state}` : ""
						]
					}), M.data.meeting.last_error ? /* @__PURE__ */ v("p", {
						className: "text-sm text-danger mt-2",
						children: ["Recognition will retry without deleting audio: ", M.data.meeting.last_error]
					}) : null] }) : null,
					/* @__PURE__ */ v("div", {
						className: "mt-3 space-y-2",
						children: [K(N.data?.segments ?? []).map((e) => e.kind === "speech" ? /* @__PURE__ */ v("div", { children: [e.timestamp ? /* @__PURE__ */ _("div", {
							className: "mb-1 mt-4 text-xs font-medium tabular-nums text-muted",
							children: e.timestamp
						}) : null, /* @__PURE__ */ _("p", {
							className: "text-sm leading-relaxed text-text",
							children: e.text
						})] }, e.key) : /* @__PURE__ */ v("div", {
							className: "flex items-center justify-between gap-3 rounded-md bg-muted/20 px-3 py-2",
							children: [/* @__PURE__ */ v("span", {
								className: "text-xs text-muted",
								children: [
									"Silence covered",
									" · ",
									(e.segment.audio_start_sample / 16e3).toFixed(1),
									"–",
									(e.segment.audio_end_sample / 16e3).toFixed(1),
									"s"
								]
							}), /* @__PURE__ */ _(a, {
								onClick: () => void H(e.segment.id),
								disabled: D !== void 0,
								children: D === e.segment.id ? "Retrying…" : "Recognize again"
							})]
						}, e.key)), N.data && N.data.segments.length === 0 ? /* @__PURE__ */ _("p", {
							className: "text-sm text-muted",
							children: "No committed speech yet."
						}) : null]
					})
				]
			}) : null
		]
	})] });
}
//#endregion
export { J as default };
