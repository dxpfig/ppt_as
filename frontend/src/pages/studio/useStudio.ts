import { useCallback, useEffect, useRef, useState } from 'react';
import type { SetStateAction } from 'react';

import { studioApi, StudioError } from './api';
import type { Health, JobRequest, StudioJob, ProjectSummary } from './api';
import { initialProject, isEmptyProject, loadProject, saveProject, uid } from './model';
import type { Project, Template } from './model';

const errorText = (error: unknown) =>
	error instanceof Error ? error.message : '请求失败，请稍后重试。';
export function useStudio() {
	const [project, updateProject] = useState(loadProject);
	const projectRef = useRef(project);
	const [health, setHealth] = useState<Health | null>(null);
	const [projects, setProjects] = useState<ProjectSummary[]>([]);
	const [lastDeleted, setLastDeleted] = useState<ProjectSummary | null>(null);
	const [templates, setTemplates] = useState<Template[]>([]);
	const [job, setJob] = useState<StudioJob | null>(null);
	const [pending, setPending] = useState(false);
	const [loading, setLoading] = useState(true);
	const [localDraft, setLocalDraft] = useState<Project | null>(null);
	const hydrateId = useRef<string | null>(null);
	const connectRun = useRef(0);
	const [error, setError] = useState('');
	const [saveState, setSaveState] = useState('正在连接服务…');
	const [connected, setConnected] = useState(false);
	const [syncTick, setSyncTick] = useState(0);
	const [uploading, setUploading] = useState(false);
	const savedRef = useRef('');
	const saveQueue = useRef<Promise<unknown>>(Promise.resolve());
	const jobSnapshot = useRef<Project | null>(null);
	const activeRef = useRef(false);
	const mounted = useRef(true);
	const setProject = useCallback((action: SetStateAction<Project>) => {
		const previous = projectRef.current;
		let next = typeof action === 'function' ? action(previous) : action;
		if (
			next.artifact === previous.artifact &&
			(next.title !== previous.title ||
				next.slides !== previous.slides ||
				next.theme !== previous.theme ||
				next.templateId !== previous.templateId ||
				next.imageSource !== previous.imageSource)
		)
			next = { ...next, artifact: undefined };
		projectRef.current = next;
		updateProject(next);
	}, []);
	const upsertSummary = useCallback((item: ProjectSummary) => {
		setProjects((previous) =>
			[item, ...previous.filter((entry) => entry.id !== item.id)].sort(
				(a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0),
			),
		);
	}, []);
	const clearSelection = useCallback(() => {
		setProject(initialProject());
		setLocalDraft(null);
		setJob(null);
		activeRef.current = false;
		jobSnapshot.current = null;
		const url = new URL(window.location.href);
		url.searchParams.delete('project');
		window.history.replaceState({}, '', url);
		try {
			localStorage.removeItem('ppt-as-active-job');
			localStorage.removeItem('ppt-as-recovery-draft');
		} catch {
			/* Optional cache. */
		}
	}, [setProject]);
	const persist = useCallback(
		(snapshot: Project) => {
			if (isEmptyProject(snapshot) || snapshot.phase === 'generating')
				return Promise.resolve();
			const serialized = JSON.stringify(snapshot);
			const task = saveQueue.current
				.catch(() => undefined)
				.then(() => studioApi.save(snapshot));
			saveQueue.current = task;
			return task
				.then((saved) => {
					savedRef.current = serialized;
					upsertSummary({
						id: saved.id,
						title: saved.title,
						slideCount: saved.slides.length,
						phase: saved.phase,
						updatedAt: (saved as Project & { updatedAt: number }).updatedAt,
					});
				})
				.catch((cause: unknown) => {
					if (cause instanceof StudioError && cause.status === 410) {
						setProjects((previous) =>
							previous.filter((item) => item.id !== snapshot.id),
						);
						if (projectRef.current.id === snapshot.id) clearSelection();
					}
					throw cause;
				});
		},
		[upsertSummary, clearSelection],
	);
	const activateProject = useCallback(
		(loaded: Project) => {
			setProject(loaded);
			setLocalDraft(null);
			if (loaded.activeJobId) {
				jobSnapshot.current = loaded;
				activeRef.current = true;
				setJob({
					id: loaded.activeJobId,
					status: 'running',
					stage: 0,
					message: '正在恢复任务进度…',
				});
			} else {
				activeRef.current = false;
				jobSnapshot.current = null;
				setJob(null);
			}
		},
		[setProject],
	);
	const reconnect = useCallback(async () => {
		const run = ++connectRun.current;
		const id = hydrateId.current;
		if (id) setLoading(true);
		setError('');
		try {
			const [state, available, records] = await Promise.all([
				studioApi.health(),
				studioApi.templates(),
				studioApi.projects(),
			]);
			let loaded: Project | null = null;
			let missingSharedProject = false;
			if (id) {
				try {
					loaded = await studioApi.project(id);
				} catch (cause) {
					if (!(cause instanceof StudioError && cause.status === 404)) throw cause;
					missingSharedProject =
						new URL(window.location.href).searchParams.get('project') === id;
				}
			}
			if (!mounted.current || run !== connectRun.current) return;
			if (missingSharedProject) clearSelection();
			if (loaded) {
				const previous = projectRef.current;
				activateProject(loaded);
				const content = (value: Project) =>
					JSON.stringify([
						value.title,
						value.slides,
						value.theme,
						value.templateId,
						value.messages,
					]);
				if (
					!loaded.activeJobId &&
					previous.phase !== 'generating' &&
					previous.id === loaded.id &&
					content(previous) !== content(loaded)
				) {
					setLocalDraft(previous);
					try {
						localStorage.setItem('ppt-as-recovery-draft', JSON.stringify(previous));
					} catch {
						/* Recovery remains in memory. */
					}
				}
			}
			hydrateId.current = null;
			setHealth(state);
			setTemplates(available);
			setProjects(records);
			setConnected(true);
			setSyncTick((value) => value + 1);
		} catch (cause) {
			if (mounted.current && run === connectRun.current) {
				setError(errorText(cause));
				setConnected(false);
			}
		} finally {
			if (mounted.current && run === connectRun.current) setLoading(false);
		}
	}, [activateProject, clearSelection]);
	useEffect(() => {
		mounted.current = true;
		const sharedId = new URL(window.location.href).searchParams.get('project');
		hydrateId.current = sharedId || projectRef.current.id;
		try {
			const stored = JSON.parse(localStorage.getItem('ppt-as-active-job') ?? 'null') as {
				id: string;
				project: Project;
			} | null;
			if (stored && (!sharedId || sharedId === stored.project.id)) {
				jobSnapshot.current = stored.project;
				setProject({ ...stored.project, phase: 'generating' });
				activeRef.current = true;
				hydrateId.current = null;
				setJob({
					id: stored.id,
					status: 'running',
					stage: 0,
					message: '正在恢复任务进度…',
				});
			}
		} catch {
			/* A missing job record starts a normal session. */
		}
		void reconnect();
		const run = connectRun.current;
		return () => {
			mounted.current = false;
			connectRun.current = run + 1;
		};
	}, [reconnect, setProject]);
	useEffect(() => {
		if (loading || pending) return;
		try {
			saveProject(project);
		} catch {
			setSaveState('浏览器备份不可用');
		}
		if (project.phase === 'generating') {
			setSaveState('生成中 · 已保存到服务器');
			return;
		}
		if (isEmptyProject(project)) {
			setSaveState(connected ? '准备开始创作' : '服务未连接');
			return;
		}
		if (!connected) {
			setSaveState('本机草稿 · 服务未连接');
			return;
		}
		if (JSON.stringify(project) === savedRef.current) {
			setSaveState('已保存到服务器');
			return;
		}
		setSaveState('正在保存…');
		const timer = setTimeout(() => {
			void persist(project)
				.then(() => {
					if (mounted.current && projectRef.current === project)
						setSaveState('已保存到服务器');
				})
				.catch((cause: unknown) => {
					if (mounted.current) {
						setSaveState('保存失败 · 已保留本机草稿');
						setError(errorText(cause));
					}
				});
		}, 600);
		return () => clearTimeout(timer);
	}, [project, connected, loading, pending, persist, syncTick]);
	const runningId = job && ['queued', 'running'].includes(job.status) ? job.id : null;
	useEffect(() => {
		if (!runningId) return;
		let disposed = false;
		let timer: ReturnType<typeof setTimeout>;
		const poll = async () => {
			try {
				const currentJob = await studioApi.job(runningId);
				if (disposed) return;
				setJob(currentJob);
				if (currentJob.status === 'queued' || currentJob.status === 'running') {
					timer = setTimeout(() => void poll(), 1000);
					return;
				}
				activeRef.current = false;
				try {
					localStorage.removeItem('ppt-as-active-job');
				} catch {
					/* Optional cache. */
				}
				const snapshot = jobSnapshot.current;
				if (!snapshot || projectRef.current.id !== snapshot.id) return;
				const result = currentJob.result;
				if (currentJob.status === 'completed' && result) {
					const next: Project = {
						...snapshot,
						slides: result.slides ?? snapshot.slides,
						title: result.title ?? snapshot.title,
						phase: result.artifact || snapshot.phase === 'ready' ? 'ready' : 'outline',
						contentGenerated:
							!!result.slides || !!result.artifact || snapshot.contentGenerated,
						artifact: result.artifact,
						messages: result.reply
							? [
									...snapshot.messages,
									{ id: uid(), role: 'assistant', text: result.reply },
								]
							: snapshot.messages,
					};
					if (result.artifact)
						next.versions = [
							{
								id: uid(),
								label: `生成版本 · ${new Date().toLocaleTimeString('zh-CN')}`,
								title: next.title,
								slides: structuredClone(next.slides),
								theme: next.theme,
								templateId: next.templateId,
								imageSource: next.imageSource,
								artifact: next.artifact,
							},
							...snapshot.versions,
						].slice(0, 12);
					setProject(result.project ?? { ...next, activeJobId: null });
					setError('');
				} else {
					setProject({
						...snapshot,
						activeJobId: null,
						phase: snapshot.artifact ? 'ready' : 'outline',
					});
					if (currentJob.status === 'failed')
						setError(currentJob.error || currentJob.message || '生成失败，请重试。');
				}
			} catch (cause) {
				if (disposed) return;
				if (cause instanceof StudioError && cause.status === 404) {
					activeRef.current = false;
					setJob({ id: runningId, status: 'failed', stage: 0, message: '任务已失效' });
					if (jobSnapshot.current)
						setProject({
							...jobSnapshot.current,
							phase: jobSnapshot.current.artifact ? 'ready' : 'outline',
						});
					try {
						localStorage.removeItem('ppt-as-active-job');
					} catch {
						/* Optional cache. */
					}
					setError('任务已失效，草稿已恢复。请重新生成。');
					return;
				}
				setError(`${errorText(cause)} 正在尝试恢复任务连接。`);
				timer = setTimeout(() => void poll(), 3000);
			}
		};
		void poll();
		return () => {
			disposed = true;
			clearTimeout(timer);
		};
	}, [runningId, setProject]);
	async function start(
		kind: JobRequest['kind'],
		options: Omit<JobRequest, 'kind' | 'project'> = {},
		nextProject = projectRef.current,
	) {
		if (loading || pending || activeRef.current || uploading) return false;
		activeRef.current = true;
		setPending(true);
		setError('');
		try {
			await persist(nextProject);
			const started = await studioApi.start({
				kind,
				project: nextProject,
				templateId: nextProject.templateId,
				...options,
			});
			jobSnapshot.current = nextProject;
			try {
				localStorage.setItem(
					'ppt-as-active-job',
					JSON.stringify({ id: started.id, project: nextProject }),
				);
			} catch {
				/* Task still runs without browser storage. */
			}
			setProject({ ...nextProject, activeJobId: started.id, phase: 'generating' });
			setJob({ ...started, status: 'queued' });
			return true;
		} catch (cause) {
			activeRef.current = false;
			setError(errorText(cause));
			return false;
		} finally {
			setPending(false);
		}
	}
	async function stop() {
		if (!runningId) return;
		try {
			await studioApi.cancel(runningId);
		} catch (cause) {
			setError(errorText(cause));
		}
	}
	async function uploadTemplate(file: File) {
		if (!/\.(pptx|potx)$/i.test(file.name)) {
			setError('请选择 PPTX 或 POTX 公司模板。');
			return;
		}
		if (file.size > 30 * 1024 * 1024) {
			setError('模板文件不能超过 30 MB。');
			return;
		}
		setUploading(true);
		setError('');
		try {
			const template = await studioApi.uploadTemplate(file);
			setTemplates((previous) => [
				template,
				...previous.filter((item) => item.id !== template.id),
			]);
			setProject((previous) => ({ ...previous, templateId: template.id }));
		} catch (cause) {
			setError(errorText(cause));
		} finally {
			setUploading(false);
		}
	}
	const busy = loading || pending || !!runningId || uploading;
	async function refreshProjects() {
		try {
			setProjects(await studioApi.projects());
		} catch (cause) {
			setError(errorText(cause));
		}
	}
	async function openProject(id: string) {
		if (busy || id === projectRef.current.id) return;
		setPending(true);
		setError('');
		try {
			await persist(projectRef.current);
			activateProject(await studioApi.project(id));
			const url = new URL(window.location.href);
			url.searchParams.set('project', id);
			window.history.replaceState({}, '', url);
		} catch (cause) {
			setError(errorText(cause));
		} finally {
			setPending(false);
		}
	}
	async function newProject() {
		if (busy) return;
		setPending(true);
		setError('');
		try {
			await persist(projectRef.current);
			clearSelection();
		} catch (cause) {
			setError(errorText(cause));
		} finally {
			setPending(false);
		}
	}
	async function deleteProject(item: ProjectSummary) {
		if (busy) return false;
		setPending(true);
		setError('');
		try {
			// Drain pending saves before deleting, so they cannot revive the record.
			await saveQueue.current.catch(() => undefined);
			await studioApi.deleteProject(item.id);
			setProjects((previous) => previous.filter((record) => record.id !== item.id));
			setLastDeleted(item);
			if (projectRef.current.id === item.id) clearSelection();
			return true;
		} catch (cause) {
			setError(errorText(cause));
			return false;
		} finally {
			setPending(false);
		}
	}
	async function undoDelete() {
		if (!lastDeleted || busy) return;
		setPending(true);
		try {
			upsertSummary(await studioApi.restoreProject(lastDeleted.id));
			setLastDeleted(null);
		} catch (cause) {
			setError(errorText(cause));
		} finally {
			setPending(false);
		}
	}

	return {
		project,
		projects,
		refreshProjects,
		deleteProject,
		lastDeleted,
		undoDelete,
		setProject,
		health,
		templates,
		job,
		error,
		setError,
		saveState,
		connected,
		uploading,
		busy,
		loading,
		canStop: !!runningId,
		start,
		stop,
		uploadTemplate,
		reconnect,
		openProject,
		newProject,
		localDraft,
		restoreLocalDraft: () => {
			if (localDraft) setProject(localDraft);
			setLocalDraft(null);
		},
		dismissLocalDraft: () => setLocalDraft(null),
	};
}
