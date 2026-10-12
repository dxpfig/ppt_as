import { useEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';

export function useMediaQuery(query: string) {
	const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
	useEffect(() => {
		const media = window.matchMedia(query);
		const update = () => setMatches(media.matches);
		update();
		media.addEventListener('change', update);
		return () => media.removeEventListener('change', update);
	}, [query]);
	return matches;
}

export function usePageWheel(
	ref: RefObject<HTMLElement | null>,
	onStep: (direction: number) => void,
	enabled: boolean,
) {
	const gesture = useRef({ last: 0, amount: 0, consumed: false });
	useEffect(() => {
		const element = ref.current;
		if (!element || !enabled) return;
		const wheel = (event: WheelEvent) => {
			if (
				event.ctrlKey ||
				event.metaKey ||
				event.shiftKey ||
				Math.abs(event.deltaX) > Math.abs(event.deltaY)
			)
				return;
			if (!event.deltaY) return;
			event.preventDefault();
			const now = performance.now();
			const state = gesture.current;
			if (now - state.last > 180) {
				state.amount = 0;
				state.consumed = false;
			}
			state.last = now;
			if (state.consumed) return;
			state.amount +=
				event.deltaY *
				(event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? element.clientHeight : 1);
			if (Math.abs(state.amount) < 45) return;
			state.consumed = true;
			onStep(Math.sign(state.amount));
		};
		element.addEventListener('wheel', wheel, { passive: false });
		return () => element.removeEventListener('wheel', wheel);
	}, [ref, enabled, onStep]);
}

export function usePresentation(
	workbench: RefObject<HTMLElement | null>,
	iframe: RefObject<HTMLIFrameElement | null>,
	projectId: string,
) {
	const [presenting, setPresenting] = useState(false);
	const [windowOnly, setWindowOnly] = useState(false);
	const returnFocus = useRef<HTMLElement | null>(null);
	const post = (data: Record<string, unknown>) =>
		iframe.current?.contentWindow?.postMessage({ ...data, projectId }, window.location.origin);
	function stop() {
		setPresenting(false);
		if (window.document.fullscreenElement === workbench.current)
			void window.document.exitFullscreen().catch(() => undefined);
		returnFocus.current?.focus();
	}
	function start() {
		returnFocus.current =
			window.document.activeElement instanceof HTMLElement
				? window.document.activeElement
				: null;
		setPresenting(true);
		setWindowOnly(false);
		if (workbench.current?.requestFullscreen) {
			void workbench.current.requestFullscreen().catch(() => setWindowOnly(true));
		} else setWindowOnly(true);
	}
	useEffect(() => {
		iframe.current?.contentWindow?.postMessage(
			{ type: 'pptd:presentation', projectId, active: presenting },
			window.location.origin,
		);
		if (presenting) iframe.current?.focus();
	}, [presenting, iframe, projectId]);
	useEffect(() => {
		const element = workbench.current;
		const change = () => {
			if (!window.document.fullscreenElement) {
				setPresenting(false);
				returnFocus.current?.focus();
			}
		};
		window.document.addEventListener('fullscreenchange', change);
		return () => {
			window.document.removeEventListener('fullscreenchange', change);
			if (element && window.document.fullscreenElement === element)
				void window.document.exitFullscreen().catch(() => undefined);
		};
	}, [workbench]);
	useEffect(() => {
		if (!presenting) return;
		const key = (event: KeyboardEvent) => {
			if (event.ctrlKey || event.metaKey || event.altKey) return;
			if (event.key === 'Escape') {
				event.preventDefault();
				stop();
			}
			if (
				[
					'ArrowRight',
					'ArrowDown',
					'PageDown',
					' ',
					'ArrowLeft',
					'ArrowUp',
					'PageUp',
				].includes(event.key)
			) {
				event.preventDefault();
				if (!event.repeat)
					post({
						type: 'pptd:navigate',
						direction: ['ArrowLeft', 'ArrowUp', 'PageUp'].includes(event.key) ? -1 : 1,
					});
			}
		};
		const receive = (event: MessageEvent) => {
			if (
				event.origin === window.location.origin &&
				event.source === iframe.current?.contentWindow &&
				event.data?.projectId === projectId &&
				event.data?.type === 'pptd:exit-presentation'
			)
				stop();
		};
		window.addEventListener('keydown', key);
		window.addEventListener('message', receive);
		return () => {
			window.removeEventListener('keydown', key);
			window.removeEventListener('message', receive);
		};
	});
	return {
		presenting,
		windowOnly,
		start,
		stop,
		sync: () => post({ type: 'pptd:presentation', active: presenting }),
		navigate: (direction: number) => post({ type: 'pptd:navigate', direction }),
	};
}
