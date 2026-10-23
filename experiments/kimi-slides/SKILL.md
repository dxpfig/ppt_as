---
name: kimi-slides
description: Create and edit presentations in PPTX format. This skill defines a .pptd intermediate format to simplify OOXML operations. Any task involving the generation or editing of PPTX files must use this skill and no other method. This skill can also be used to read uploaded PPTX files and convert PPTX documents into images.
---

# Definition
kimi-slides is a ppt-generation skill built by Moonshot AI as a first-party skill. It defines a YAML-format intermediate DSL (.pptd) that further abstracts OOXML, making presentation generation effortless. The DSL can be used to generate and render PPTs, and existing pptx files can also be converted into this DSL for editing.

## The pptd format
The .pptd format is a simplified abstraction layer over OOXML that follows basic YAML syntax. This abstraction preserves the core content of OOXML (theme, page layout, element positions and definitions, etc.) while removing complex nesting logic such as Masters; every page is self-contained — what you see is what you get. Read reference/pptd.md for the complete definition of this DSL.

## Companion CLI
The skill also ships with a companion CLI tool (pre-installed in the environment) for two-way conversion between pptd and pptx, pptd validation, pptd/pptx screenshot rendering, and more.
Read reference/cli.md for the complete CLI usage instructions.

## PPT production workflow

### step1. Read the context thoroughly
Read **all information provided by the user**, including uploaded files, mentioned URLs, and other context, to fully understand the user's requirements

### step2. Understand the user's requirements
Understand the user's requirements based on the context:
1. First determine the request type
  - Create a PPT: create a new presentation (from scratch, or from an existing pptx template)
  - Edit a PPT: edit the user's uploaded PPT (local modifications, single-page beautification, etc.)
  - Replicate a PPT: replicate a presentation from a non-pptx format (images, PDF, etc.) into pptx format

2. Then determine the design method
  - Self-directed design: no preference, or only simple style constraints given; you need to fill in or create the design
  - Design system: the user provides a complete and detailed design scheme covering all color, font, layout, and component specifications
  - Use a template: a template is provided and must be used
  - Style transfer: a style reference source is provided (images, web pages, etc.)

3. Then determine the input type
  - Topic only: only a PPT topic direction or content requirements for the presentation are given, with no concrete content
  - Full document: the user provides a complete document (paper, research report, press release, etc.)
  - Outline: the user provides a page-by-page outline, speech script, or similar content
  * When the "user input type" is [Full document] or [Outline] and it is not specified whether expansion is allowed: since a page-by-page outline, speech script, or user document can hardly support the full content of a presentation, prefer using search to expand with more relevant material, cases, etc., unless the user explicitly says not to expand

4. Finally determine the exact page count
  - If the user requests a specific page count, the user's requirement takes priority
  - Page-by-page outline/script provided: match the number of pages in the outline/script
  - When a complete and relatively structured document is provided / when only a topic is provided: decide the page count yourself based on the document content / search results

### step3. Generate the presentation based on the user's requirements

Before generating, first read `reference/pptd.md` and `reference/cli.md` to understand the pptd format definition and constraints and how to use the CLI

#### Replicating a PPT
- Analyze the images to estimate element positions, fonts and sizes, etc., and **replicate 1:1 as closely as possible**.
- For parts that are difficult to make out, use methods such as grid lines and close-up views to improve understanding.
- Replicate simple content in the image with elements; icons may be approximated with icons provided by Font Awesome. For content that cannot be approximated with icons or shapes, such as photos and avatars, use tools such as bash or python to crop and split the original image, then add the resulting image elements to the presentation

#### Editing a PPT
- Convert the user's uploaded pptx file to .pptd format
- Take screenshots of the converted presentation, then use python to stitch and compress all pages for an overview. Read difficult-to-discern key pages individually afterwards.
- Determine the editing task to perform (pages, elements, and actions), then carry it out. Repetitive changes, such as adjusting a logo's position across multiple pages, may be performed in batches with bash commands such as grep and cat.
> `kimi-slides convert` is not lossless. If the user later reports page corruption or style errors outside the area you modified, parse the original pptx file for a more detailed assessment of its styles, then make the corresponding repairs.

#### Generating a PPT
Choose the generation method based on the [design method] determined in step2

##### Self-directed design
1. Read the design guide `reference/slides_categories.md`, and read the scenario document corresponding to the user's query
2. Produce the presentation based on the above

##### Design system
1. Read the general constraints section of the `reference/slides_categories.md` guide, and read the scenario document corresponding to the user's query as the design foundation
2. Read the user-provided design system document as the presentation style. It is strictly forbidden to reference or mix in other design styles
3. Produce the presentation with reference to the above

##### Using a template
1. Use `kimi-slides convert` to convert the user's uploaded pptx file into pptd form
2. Take screenshots of the converted file, then stitch and compress the screenshots for an overview to understand the template's visual style (color scheme, font style, element characteristics, layout characteristics, content density, etc.)
3. Identify page types; focus on reading special pages such as the cover, summary pages, and section dividers (single-page screenshots, .page files), extracting their page layouts, content structures, reusable components (illustrations, background images, icons, shapes, smartart, reusable body layout schemes, etc.), and element styles (e.g., whitespace/line/card separators, square/rounded corners, etc.)
4. Produce the presentation using the template

##### Style transfer
1. Analyze the reference file's visual style (color scheme, font style, element characteristics, layout characteristics, content density, etc.), page layouts, content structures, reusable components (icons, shapes, smartart, reusable body layout schemes, etc.), and element styles (e.g., whitespace/line/card separators, square/rounded corners, etc.).
- If the user provides a style reference URL, do not only read the text content; refer to and learn from the page's visual effect more to help understand the style
2. Produce the presentation using the reference file's style characteristics. You are encouraged to reuse illustrations, fonts, font-size hierarchies, elements, etc. from the original pdf/url

##### Images and Visual Materials
1. Images are an effective way to enrich a presentation's visual impact. Appropriate images should be used not only on covers and section dividers, but also on body pages to enrich the page, aid understanding, or support decision-making
2. Images are used to show concrete subjects, explain content, provide evidence, or establish a scene. Logos, icons, decorative textures, and very small thumbnails do not count as substantive imagery.
3. When a page involves products, people, places, buildings, events, cases, interfaces, experimental subjects, or spatial environments, prioritize corresponding real images or screenshots. If real images and screenshots cannot be obtained, generated images may be used instead.
4. Image priority: images provided by the user; images from official websites, official reports, and credible sources; searched images that are directly relevant to the content; images generated for conceptual expression or atmosphere.
5. After deciding which images are needed, complete image search, generation, and downloading in a batch before designing pages around their proportions. Save images in the `media` directory, keep them clear, and never stretch or distort them.
6. Analytical, technical, and academic PPTs should use corresponding evidence images when products, experiments, interfaces, cases, or on-site materials are available. Do not reduce every page to text, color blocks, and shapes.
8. Do not add irrelevant images merely to meet a quantity target. Every image must be directly relevant to the page's conclusion or communication goal.

##### Content Guidelines
1. Language style: unless the user explicitly requests otherwise, strictly avoid overly abstract expressions and uncommon metaphors
- Do not overuse metaphors, slogans, or abstract jargon such as distribution, an N-step argument, everything at a glance, a closed loop, hands-on practice, verification, deconstruction, second-class citizens, poison pills, or wall clocks
- Do not use common AI phrasing such as “not X, but Y,” “X is Y,” “why / based on what / how,” “key takeaway,” or “N battlefronts / paths”
- Do not use overly colloquial expressions such as “where should the ammunition go,” “the Nth thing,” “can't pick the right one,” or “cannot be used as X”

### step4. PPT validation
1. Use the `kimi-slides check` command to validate and repair the generated file over multiple rounds
  - Interpret the results returned by `check` carefully. Some warnings may be produced by heuristic calculations and may contain errors; use the actual design as the basis for judgment
  - Ignore intentional design choices such as bleed effects, deliberately overlapping text, or content extending beyond the page margins.
2. If the user reports issues beyond what `kimi-slides check` covers, use `kimi-slides screenshot` to take screenshots, refine the problematic pages, and run multiple rounds of validation and repair

> The `screenshot` command uses simulated rendering, which may differ from the actual result in the editor. Follow the semantics defined in pptd.md.

### step5. PPT delivery
1. Delivery format: deliver the presentation in `.pptd` format so the user can edit it and continue refining it across multiple rounds of conversation.
2. Delivery method: **at the end of every response, always output a `kimi_ref` placeholder in the following format** to deliver the `.pptd` file to the user:
  - Format: `<KIMI_REF type="kimi-slides" path="/abs/path/<file_name>.pptd" />`
  - `type` must be `kimi-slides`, and `path` must point to the `.pptd` file using an absolute path.
3. Delivery result: after the `kimi_ref` placeholder is output, it is rendered as a PPT card in the app. The user can click the card to open the editor, where the presentation can be edited and annotated through the GUI. The only way to export it as a PPTX or image is to click the export button in the upper-right corner of the editor. Do not use any other method to export a PPTX.
4. If the user strongly requests a PPTX export, direct them to export it from the editor. If, after multiple rounds of feedback, they still cannot export it, cannot find the editor, or cannot find the export button, direct them to submit feedback. Do not use the `kimi-slides` command to export a PPTX.

## Important notes
1. Artifact location: the .pptd file must not be written to directories such as `/tmp` or `/work`
2. Animation usage: use animations only when the user explicitly requests them, or when the PPT is clearly intended for live presentation or slideshow playback and animation provides a clear benefit for staged disclosure, process demonstration, causal explanation, pacing, visual impact, or brand storytelling; by default, do not add animations to reading-oriented, self-study, print, or primarily send-and-browse PPTs.
3. Speaker notes (`note`) usage: use them only when the user explicitly requests them; otherwise, do not add them.
4. Parallel tool calls: during PPT production, make tool calls in parallel whenever possible; in each round, invoke the `write_file tool` multiple times in parallel to write more pages and reduce the number of steps.
5. If the user asks for revisions over multiple turns, output kimi_ref in every turn. **If kimi_ref is omitted, the user cannot see the asset card and therefore cannot edit, preview, or export the asset.**
6. The skill includes built-in version management. When the output kimi_ref points to the same .pptd file, the system automatically keeps each version, and the user can switch between versions in the frontend to preview or restore them. **Do not move the .pptd file or its dependencies unless necessary.**
