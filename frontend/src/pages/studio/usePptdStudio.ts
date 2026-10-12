import { useCallback, useEffect, useRef, useState } from 'react';

import { studioApi, StudioError } from './api';
import type { DesignSystemSummary, Health, JobRequest, ProjectSummary, StudioJob } from './api';
import { initialProject, isEmptyProject, loadProject, saveProject, uid } from './model';
import type { PptdDocument, Project, Template } from './model';
import { acceptServerProject, isRunningJob } from './pptdState';

const errorText = (cause: unknown) =>
	cause instanceof Error ? cause.message : '操作失败，请稍后重试。';
const ACTIVE_JOB = 'ppt-as-pptd-active-job';
function cacheProject(project: Project) {
	try {
		saveProject(project);
	} catch {
		/* The server remains authoritative. */
	}
}
function cacheJob(job: StudioJob | null, project: Project) {
	try {
		if (job)
			localStorage.setItem(ACTIVE_JOB, JSON.stringify({ id: job.id, projectId: project.id }));
		else localStorage.removeItem(ACTIVE_JOB);
	} catch {
		/* Server recovery also uses activeJobId. */
	}
}
function projectUrl(id?: string) {
	const url = new URL(window.location.href);
	if (id) url.searchParams.set('project', id);
	else url.searchParams.delete('project');
	window.history.replaceState({}, '', url);
}
export function usePptdStudio() {
	const [project, setProject] = useState(loadProject);
	const projectRef = useRef(project);
	const [projects, setProjects] = useState<ProjectSummary[]>([]);
	const [health, setHealth] = useState<Health | null>(null);
	const [designSystems, setDesignSystems] = useState<DesignSystemSummary[]>([]);
	const [templates, setTemplates] = useState<Template[]>([]);
	const [document, setDocument] = useState<PptdDocument | null>(null);
	const [job, setJob] = useState<StudioJob | null>(null);
	const [loading, setLoading] = useState(true);
	const [pending, setPending] = useState(false);
	const [connected, setConnected] = useState(false);
	const [error, setError] = useState('');
	const [lastDeleted, setLastDeleted] = useState<ProjectSummary | null>(null);
	const operationRef = useRef(false);
	const runningId = isRunningJob(job) ? job?.id : undefined;
	const busy = loading || pending || !!runningId;
	const documentRevision = project.pptd?.revision;
	const adopt = useCallback((incoming: Project) => {
		const next = acceptServerProject(projectRef.current, incoming);
		projectRef.current = next;
		setProject(next);
		cacheProject(next);
		return next;
	}, []);
	const refreshProjects = useCallback(async () => {
		try {
			setProjects(await studioApi.projects());
		} catch (cause) {
			setError(errorText(cause));
		}
	}, []);
	const refreshProject = useCallback(async () => {
		const id = projectRef.current.id;
		const incoming = await studioApi.project(id);
		if (projectRef.current.id !== id) return;
		adopt(incoming);
		await refreshProjects();
	}, [adopt, refreshProjects]);
	const reconnect = useCallback(async () => {
		setError('');
		const results = await Promise.allSettled([
			studioApi.health(),
			studioApi.projects(),
			studioApi.templates(),
			studioApi.designSystems(),
		]);
		const [healthResult, recordsResult, templatesResult, designSystemsResult] = results;
		if (healthResult.status === 'fulfilled') {
			setHealth(healthResult.value);
			setConnected(true);
		} else {
			setConnected(false);
			setError(errorText(healthResult.reason));
		}
		if (recordsResult.status === 'fulfilled') setProjects(recordsResult.value);
		else setError(errorText(recordsResult.reason));
		if (templatesResult.status === 'fulfilled') setTemplates(templatesResult.value);
		if (designSystemsResult.status === 'fulfilled')
			setDesignSystems(designSystemsResult.value);
	}, []);
	useEffect(() => {
		let disposed = false;
		async function hydrate() {
			await reconnect();
			const requestedId = new URL(window.location.href).searchParams.get('project');
			const cached = projectRef.current;
			const id = requestedId || (!isEmptyProject(cached) ? cached.id : null);
			try {
				if (id) {
					const incoming = await studioApi.project(id);
					if (disposed) return;
					adopt(incoming);
					let jobId = incoming.activeJobId || incoming.lastJobId;
					if (!jobId) {
						try {
							const cachedJob = JSON.parse(
								localStorage.getItem(ACTIVE_JOB) ?? 'null',
							) as { id?: string; projectId?: string } | null;
							if (cachedJob?.projectId === id) jobId = cachedJob.id;
						} catch {
							/* Optional browser cache. */
						}
					}
					if (jobId) {
						try {
							const recovered = await studioApi.job(jobId);
							if (!disposed) setJob(recovered);
						} catch (cause) {
							if (!disposed) setError(errorText(cause));
						}
					}
				}
			} catch (cause) {
				if (!disposed) setError(errorText(cause));
			} finally {
				if (!disposed) setLoading(false);
			}
		}
		void hydrate();
		return () => {
			disposed = true;
		};
	}, [adopt, reconnect]);
	useEffect(() => {
		let disposed = false;
		setDocument(null);
		if (documentRevision !== undefined) {
			void studioApi
				.pptd(project.id)
				.then((bundle) => {
					if (!disposed) setDocument(bundle);
				})
				.catch((cause: unknown) => {
					if (!disposed) setError(errorText(cause));
				});
		}
		return () => {
			disposed = true;
		};
	}, [project.id, documentRevision]);
	useEffect(() => {
		if (!runningId) return;
		let disposed = false;
		let timer: ReturnType<typeof setTimeout>;
		let completing = false;
		let stream: EventSource | null = null;
		let polling = false;
		const projectId = projectRef.current.id;
		async function apply(next: StudioJob) {
			if (disposed || completing) return;
			if (isRunningJob(next)) {
				setError('');
				setJob(next);
				return;
			}
			completing = true;
			stream?.close();
			clearTimeout(timer);
			try {
				const saved = next.result?.project ?? (await studioApi.project(projectId));
				if (disposed || projectRef.current.id !== projectId) return;
				adopt(saved);
				await refreshProjects();
				if (next.status === 'failed')
					setError(next.error || next.message || '生成失败，可以重试。');
				else setError('');
				cacheJob(next.status === 'completed' ? null : next, saved);
			} catch (cause) {
				if (!disposed) setError(errorText(cause));
			} finally {
				if (!disposed) {
					setJob(next);
					completing = false;
				}
			}
		}
		async function poll() {
			try {
				await apply(await studioApi.job(runningId!));
			} catch (cause) {
				if (
					!disposed &&
					cause instanceof StudioError &&
					[404, 410].includes(cause.status)
				) {
					setJob({
						id: runningId!,
						status: 'interrupted',
						stage: 0,
						message: '任务记录已失效，可根据保留的内容重新生成。',
						recoverable: false,
					});
					cacheJob(null, projectRef.current);
					setError('任务记录已失效，已保留当前文档。');
					return;
				}
				if (!disposed) setError(`${errorText(cause)} 正在恢复任务连接…`);
			}
			if (!disposed) timer = setTimeout(() => void poll(), 1500);
		}
		function fallbackToPolling() {
			if (disposed || polling || completing) return;
			polling = true;
			stream?.close();
			clearTimeout(timer);
			void poll();
		}
		if (typeof EventSource === 'undefined') fallbackToPolling();
		else {
			try {
				stream = new EventSource(
					`/api/studio/jobs/${encodeURIComponent(runningId)}/events`,
				);
				stream.onmessage = (event) => {
					try {
						const next = JSON.parse(event.data) as StudioJob;
						if (next.id !== runningId || !next.status)
							throw new Error('Invalid job event');
						clearTimeout(timer);
						void apply(next);
					} catch {
						fallbackToPolling();
					}
				};
				stream.onerror = fallbackToPolling;
				timer = setTimeout(fallbackToPolling, 5000);
			} catch {
				fallbackToPolling();
			}
		}
		return () => {
			disposed = true;
			stream?.close();
			clearTimeout(timer);
		};
	}, [runningId, adopt, refreshProjects]);

	async function runAction(action: () => Promise<void>) {
		if (operationRef.current || busy) return false;
		operationRef.current = true;
		setPending(true);
		setError('');
		try {
			await action();
			return true;
		} catch (cause) {
			setError(errorText(cause));
			if (cause instanceof StudioError && cause.status === 409) {
				try {
					await refreshProject();
				} catch {
					/* Preserve the conflict explanation. */
				}
			}
			return false;
		} finally {
			operationRef.current = false;
			setPending(false);
		}
	}
	async function start(
		kind: JobRequest['kind'],
		options: Omit<JobRequest, 'kind' | 'project'> = {},
	) {
		return runAction(async () => {
			const current = projectRef.current;
			// Two company-template routes share this switch: "apply" keeps the
			// template's own pages and rewrites only their text (its own job kind and
			// a published PPTX), while "rebuild" composes new pages from the
			// template's spec and commits an ordinary PPTD document. The caller
			// chooses by passing the job kind, so only an explicit 'template' is
			// redirected here.
			const templateRoute =
				kind === 'template' && options.designMode === 'template' && !!options.templateId;
			const snapshot: Project = {
				...current,
				workflow: 'pptd-v2',
				...(templateRoute ? { templateId: options.templateId } : {}),
				messages: options.prompt
					? [...current.messages, { id: uid(), role: 'user', text: options.prompt }]
					: current.messages,
			};
			const nextJob = await studioApi.start({
				kind: templateRoute ? 'template' : kind,
				project: snapshot,
				expectedRevision: snapshot.pptd?.revision,
				...options,
				selectedSlideId: templateRoute ? undefined : options.selectedSlideId,
			});
			const active = adopt({ ...snapshot, activeJobId: nextJob.id, phase: 'generating' });
			cacheJob(nextJob, active);
			projectUrl(active.id);
			setJob(nextJob);
		});
	}
	async function openProject(id: string) {
		return runAction(async () => {
			const incoming = await studioApi.project(id);
			adopt(incoming);
			projectUrl(id);
			setJob(null);
			const jobId = incoming.activeJobId || incoming.lastJobId;
			setJob(jobId ? await studioApi.job(jobId) : null);
		});
	}
	function newProject() {
		if (busy) return;
		adopt({ ...initialProject(), workflow: 'pptd-v2' });
		setJob(null);
		setDocument(null);
		setError('');
		cacheJob(null, projectRef.current);
		projectUrl();
	}
	async function stop() {
		if (!runningId) return;
		try {
			const stopped = await studioApi.cancel(runningId);
			if (!isRunningJob(stopped)) {
				await refreshProject();
				cacheJob(stopped, projectRef.current);
			}
			setJob(stopped);
		} catch (cause) {
			setError(errorText(cause));
		}
	}
	async function retry(resume = false) {
		if (!job) return;
		return runAction(async () => {
			const resumed = await (resume ? studioApi.resume(job.id) : studioApi.retry(job.id));
			cacheJob(resumed, projectRef.current);
			adopt({ ...projectRef.current, activeJobId: resumed.id, phase: 'generating' });
			setJob(resumed);
		});
	}
	async function migrate() {
		return runAction(async () => {
			await studioApi.migratePptd(projectRef.current.id);
			await refreshProject();
		});
	}
	async function restore(versionId: string) {
		return runAction(async () => {
			const current = projectRef.current;
			if (!current.pptd) return;
			await studioApi.restorePptd(current.id, versionId, current.pptd.revision);
			await refreshProject();
		});
	}
	async function deleteProject(item: ProjectSummary) {
		return runAction(async () => {
			await studioApi.deleteProject(item.id);
			setProjects((previous) => previous.filter((record) => record.id !== item.id));
			setLastDeleted(item);
			if (item.id === projectRef.current.id) {
				adopt({ ...initialProject(), workflow: 'pptd-v2' });
				setJob(null);
				setDocument(null);
				projectUrl();
			}
		});
	}
	async function undoDelete() {
		if (!lastDeleted) return;
		return runAction(async () => {
			await studioApi.restoreProject(lastDeleted.id);
			setLastDeleted(null);
			await refreshProjects();
		});
	}
	async function uploadTemplate(file: File) {
		let uploaded: Template | undefined;
		await runAction(async () => {
			if (!/\.(pptx|potx)$/i.test(file.name) || file.size > 30 * 1024 * 1024)
				throw new Error('请选择不超过 30 MB 的 PPTX 或 POTX 模板。');
			uploaded = await studioApi.uploadTemplate(file);
			setTemplates((previous) => [
				uploaded!,
				...previous.filter((item) => item.id !== uploaded!.id),
			]);
		});
		return uploaded;
	}
	return {
		project,
		projects,
		health,
		templates,
		designSystems,
		document,
		job,
		loading,
		pending,
		connected,
		error,
		setError,
		lastDeleted,
		busy,
		canStop: !!runningId,
		start,
		stop,
		retry,
		resume: () => retry(true),
		migrate,
		restore,
		newProject,
		openProject,
		deleteProject,
		undoDelete,
		refreshProjects,
		refreshProject,
		reconnect,
		uploadTemplate,
	};
}
