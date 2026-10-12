from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Layout = Literal["auto", "cover", "editorial", "image", "comparison", "process", "closing"]
ImageIntent = Literal["auto", "photo", "diagram", "none"]
Scenario = Literal["general", "management-report", "analysis-decision", "business-plan", "tech-engineering", "academic-research", "education-training", "brand-creative"]


class SlideImage(BaseModel):
    assetId: str = Field(pattern=r"^[a-f0-9]{64}$")
    url: str = Field(max_length=300)
    title: str = Field(default="配图", max_length=500)
    provider: str = Field(max_length=40)
    sourceUrl: str = Field(default="", max_length=3000)
    author: str = Field(default="", max_length=500)
    license: str = Field(default="来源未标注许可", max_length=500)
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class ImageSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    source: Literal["auto", "knowledge", "web"] = "auto"
    limit: int = Field(default=4, ge=1, le=6)


class Slide(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(min_length=1, max_length=120)
    title: str = Field(max_length=300)
    subtitle: str = Field(default="", max_length=1000)
    points: list[Annotated[str, Field(max_length=3000)]] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=12000)
    imageQuery: str = Field(default="", max_length=200)
    imageDisabled: bool = False
    image: SlideImage | None = None
    layout: Layout = "auto"
    section: str = Field(default="", max_length=60)
    source: str = Field(default="", max_length=800)
    imageIntent: ImageIntent = "auto"


class Project(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(default="未命名演示", max_length=300)
    slides: list[Slide] = Field(default_factory=list, max_length=60)
    messages: list[dict] = Field(default_factory=list, max_length=1000)
    theme: Literal["forest", "ink", "clay"] = "forest"
    phase: Literal["outline", "generating", "ready"] = "outline"
    versions: list[dict] = Field(default_factory=list, max_length=100)
    pageCount: int | None = Field(default=None, ge=1, le=60)
    audience: str = Field(default="团队分享", max_length=1000)
    references: str = Field(default="", max_length=100000)
    imageSource: Literal["auto", "knowledge", "web", "none"] = "auto"
    activeJobId: str | None = None
    templateId: str | None = None
    artifact: dict | None = None
    workflow: Literal["legacy", "pptd-v2"] = "legacy"
    pptd: dict | None = None


class JobRequest(BaseModel):
    # "template" is the surgical company-template route: keep the template's own
    # pages and change only their text (see agent_service/template_job.py).
    kind: Literal["outline", "revise", "render", "template"]
    project: Project
    prompt: str = Field(default="", max_length=30000)
    pageCount: int | None = Field(default=None, ge=1, le=60)
    audience: str = Field(default="通用汇报", max_length=1000)
    selectedSlideId: str | None = None
    references: str = Field(default="", max_length=100000)
    templateId: str | None = None
    workflow: Literal["new", "edit", "recreate"] = "new"
    designMode: Literal["autonomous", "design-system", "template", "style-reference"] = "autonomous"
    usage: Literal["reading", "presentation"] = "reading"
    allowResearch: bool = True
    speakerNotes: bool = False
    animationRequested: bool = False
    designSystem: str = Field(default="", max_length=100000)
    scenario: str = Field(default="auto", max_length=80)
    attachments: list[dict] = Field(default_factory=list, max_length=30)
    expectedRevision: int | None = Field(default=None, ge=0)
    outputMode: Literal["auto", "adaptive", "direct"] = "auto"


class PlannedPage(BaseModel):
    title: str
    goal: str
    layout: Layout = "editorial"
    imageIntent: ImageIntent = "none"


class Plan(BaseModel):
    title: str
    narrative: str
    scenario: Scenario = "general"
    visualDirection: str = ""
    pages: list[PlannedPage] = Field(min_length=1, max_length=60)


class GeneratedSlide(BaseModel):
    imageQuery: str = Field(default="", max_length=200, description="适合本页主体与场景的图片检索关键词，优先用3至6个英文词，不要URL，不要通用装饰图")
    title: str = Field(max_length=300)
    subtitle: str = Field(default="", max_length=1000)
    points: list[str] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=12000)
    layout: Layout = "editorial"
    section: str = Field(default="", max_length=60)
    source: str = Field(default="", max_length=800)
    imageIntent: ImageIntent = "none"


class DeckContent(BaseModel):
    title: str = Field(max_length=300)
    slides: list[GeneratedSlide] = Field(min_length=1, max_length=60)
    reply: str = "已完成演示内容。"
