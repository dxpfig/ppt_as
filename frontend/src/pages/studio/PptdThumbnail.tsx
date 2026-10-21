import { useEffect, useRef, useState } from 'react';

type PreviewSources = { image?: string; view: string };

export function PptdThumbnail({ sources, size }: { sources: PreviewSources; size: unknown }) {
	const container = useRef<HTMLSpanElement>(null);
	const [nearby, setNearby] = useState(false);
	const [imageFailed, setImageFailed] = useState(false);
	useEffect(() => {
		const element = container.current;
		if (!element) return;
		if (!('IntersectionObserver' in window)) {
			setNearby(true);
			return;
		}
		const observer = new IntersectionObserver(([entry]) => setNearby(entry.isIntersecting), {
			root: element.closest('.pptd-page-list'),
			rootMargin: '24px 0px',
			threshold: 0.01,
		});
		observer.observe(element);
		return () => observer.disconnect();
	}, []);
	const aspectRatio =
		Array.isArray(size) &&
		size.length === 2 &&
		size.every((value) => typeof value === 'number' && Number.isFinite(value) && value > 0)
			? `${size[0]} / ${size[1]}`
			: '16 / 9';
	return (
		<span ref={container} className="pptd-thumbnail" style={{ aspectRatio }} aria-hidden="true">
			{sources.image && !imageFailed ? (
				<img
					src={sources.image}
					alt=""
					loading="lazy"
					onError={() => setImageFailed(true)}
				/>
			) : nearby && sources.view ? (
				<LiveThumbnail key={sources.view} src={sources.view} />
			) : (
				<span className="pptd-thumbnail-placeholder">页面预览</span>
			)}
		</span>
	);
}

function LiveThumbnail({ src }: { src: string }) {
	const frame = useRef<HTMLIFrameElement>(null);
	const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
	useEffect(() => {
		const timeout = window.setTimeout(() => setStatus('error'), 30000);
		const onMessage = (event: MessageEvent) => {
			if (
				event.source !== frame.current?.contentWindow ||
				event.origin !== window.location.origin
			)
				return;
			if (event.data?.type === 'pptd:thumbnail-ready') {
				window.clearTimeout(timeout);
				setStatus('ready');
			} else if (event.data?.type === 'pptd:thumbnail-error') {
				window.clearTimeout(timeout);
				setStatus('error');
			}
		};
		window.addEventListener('message', onMessage);
		return () => {
			window.clearTimeout(timeout);
			window.removeEventListener('message', onMessage);
		};
	}, []);
	return (
		<>
			<iframe
				ref={frame}
				src={src}
				title="页面缩略预览"
				inert
				tabIndex={-1}
				data-ready={status === 'ready'}
			/>
			{status !== 'ready' && (
				<span className="pptd-thumbnail-placeholder">
					{status === 'error' ? '预览暂不可用' : '正在加载…'}
				</span>
			)}
		</>
	);
}
