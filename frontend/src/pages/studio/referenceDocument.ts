export const REFERENCE_FILE_ACCEPT = '.txt,.md,.markdown,.csv,.json,.docx';
export const REFERENCE_FILE_MAX_BYTES = 50 * 1024 * 1024;

const textExtensions = ['.txt', '.md', '.markdown', '.csv', '.json'];
const docxExtension = '.docx';

type MammothResult = {
	value: string;
	messages?: { type?: string; message?: string }[];
};

type MammothModule = {
	extractRawText: (input: { arrayBuffer: ArrayBuffer }) => Promise<MammothResult>;
};

export type ReferenceDocument = {
	text: string;
	warnings: string[];
};

function extensionOf(name: string) {
	const match = name.toLowerCase().match(/\.[^.]+$/);
	return match?.[0] ?? '';
}

export function isSupportedReferenceFile(file: File) {
	const extension = extensionOf(file.name);
	return extension === docxExtension || textExtensions.includes(extension);
}

function normalizeText(text: string) {
	return text
		.replace(/\r\n?/g, '\n')
		.replaceAll('\u0000', '')
		.replace(/[ \t]+\n/g, '\n')
		.replace(/\n{3,}/g, '\n\n')
		.trim();
}

async function readDocx(file: File): Promise<ReferenceDocument> {
	// Mammoth's browser alias keeps DOCX unzipping and XML parsing in the
	// browser. Load it only when a DOCX is selected so the main bundle stays
	// smaller for users who only upload plain text.
	const imported = (await import('mammoth')) as unknown as MammothModule & {
		default?: MammothModule;
	};
	const mammoth = imported.default ?? imported;
	const result = await mammoth.extractRawText({ arrayBuffer: await file.arrayBuffer() });
	return {
		text: normalizeText(result.value),
		warnings: (result.messages ?? [])
			.filter((message) => message.type === 'warning' && message.message)
			.map((message) => message.message!),
	};
}

/**
 * Read a reference document as plain text for the PPT agent.
 *
 * DOCX extraction intentionally uses Mammoth's raw-text mode: paragraph,
 * table and list order come from WordprocessingML, while fonts, colours and
 * layout that cannot be represented in the references textarea are ignored.
 */
export async function readReferenceDocument(
	file: File,
	maxBytes = REFERENCE_FILE_MAX_BYTES,
): Promise<ReferenceDocument> {
	if (!isSupportedReferenceFile(file))
		throw new Error('参考资料支持 TXT、Markdown、CSV、JSON 和 DOCX 文件。');
	if (file.size > maxBytes)
		throw new Error(`参考资料请控制在 ${Math.round(maxBytes / 1024 / 1024)} MB 以内。`);

	const extension = extensionOf(file.name);
	const document =
		extension === docxExtension
			? await readDocx(file)
			: { text: normalizeText(await file.text()), warnings: [] };
	if (!document.text) throw new Error('文件中没有可读取的文本内容。');
	return document;
}
