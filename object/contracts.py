"""Validated contracts shared by API, pipeline and revision service."""
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator

class Options(BaseModel):
    model_config = ConfigDict(extra='forbid')
    handwritten: bool = False
    diarization: bool = False
    output_form: Literal['both','notes','transcript'] = 'both'
    template: Literal['course','revision','meeting','summary'] = 'course'
    detail: Literal['brief','normal','detailed'] = 'normal'
    include_images: bool = True
    include_review_questions: bool = True
    degraded_mode: bool = False
    style: str = Field(default='', max_length=2000)
    instructions: str = Field(default='', max_length=4000)
    max_pages: int = Field(default=0, ge=0, le=3000)
    visual_mode: Literal['auto','always'] = 'auto'

class MaterialInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    file_id: str = Field(min_length=1)
    source_type: Literal['auto','personal_notes','lecture_slides','paper','textbook','exercise','meeting','general'] = 'auto'
    role: Literal['primary','supplementary','style_reference'] = 'primary'

class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    file_ids: list[str] = Field(default_factory=list, max_length=30)
    materials: list[MaterialInput] = Field(default_factory=list, max_length=30)
    options: Options = Field(default_factory=Options)

    @model_validator(mode='after')
    def resolve_file_ids(self):
        material_ids=[item.file_id for item in self.materials]
        if material_ids:
            if self.file_ids and self.file_ids!=material_ids:
                raise ValueError('file_ids 必须与 materials 中的文件顺序一致')
            self.file_ids=material_ids
        if not self.file_ids:
            raise ValueError('至少需要一份材料')
        return self

class RetryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    skip_review_questions: bool = False
    degraded_mode: bool = False

class TranscriptEdit(BaseModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=12000)

class TranscriptEditRequest(BaseModel):
    expected_version: int = Field(ge=1)
    segments: list[TranscriptEdit] = Field(default_factory=list, max_length=5000)


class ModelRouteInput(BaseModel):
    model: str = Field(default='', max_length=160)
    base_url: str = Field(default='', max_length=500)
    api_key: str = Field(default='', max_length=2000)

class ModelRouteOverride(ModelRouteInput):
    use_default: bool = True

class RouterSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    default: ModelRouteInput = Field(default_factory=ModelRouteInput)
    cards: ModelRouteOverride = Field(default_factory=ModelRouteOverride)
    outline: ModelRouteOverride = Field(default_factory=ModelRouteOverride)
    writer: ModelRouteOverride = Field(default_factory=ModelRouteOverride)
    vision: ModelRouteOverride = Field(default_factory=ModelRouteOverride)
    asr: ModelRouteInput = Field(default_factory=ModelRouteInput)

class SourceBlock(BaseModel):
    id: str
    document_id: str
    document_name: str
    page: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    speaker_id: int | None = None
    asr_sentences: list[dict] = Field(default_factory=list)
    text: str
    kind: Literal['text','table','visual','audio'] = 'text'
    method: str = 'native'
    asset: str | None = None
    evidence_asset: str | None = None
    bbox: list[float] | None = None
    page_layout: list[dict] = Field(default_factory=list)
    asset_role: Literal['content','source_page'] = 'content'
    uncertain: bool = False
    material_type: Literal['personal_notes','lecture_slides','paper','textbook','exercise','meeting','general'] = 'general'
    material_role: Literal['primary','supplementary'] = 'primary'

class Card(BaseModel):
    id: str
    title: str = Field(min_length=1)
    points: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    importance: int = Field(default=3, ge=1, le=5)
    formulas: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)

class ClaimContext(BaseModel):
    datasets: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    setup: list[str] = Field(default_factory=list)
    values: list[str] = Field(default_factory=list)
    negated: bool = False

class Claim(BaseModel):
    id: str
    statement: str = Field(min_length=1)
    claim_type: Literal['fact','definition','mechanism','result','limitation','decision','proposal','action','interpretation','extension'] = 'fact'
    source_ids: list[str] = Field(min_length=1)
    conditions: list[str] = Field(default_factory=list)
    context: ClaimContext = Field(default_factory=ClaimContext)
    confidence: float = Field(default=1.0, ge=0, le=1)
    importance: int = Field(default=3, ge=1, le=5)

class Concept(BaseModel):
    id: str
    name: str = Field(min_length=1)
    definition: str = ''
    claim_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)

class Formula(BaseModel):
    id: str
    latex: str = Field(min_length=1)
    meaning: str = ''
    variables: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)

class Experiment(BaseModel):
    id: str
    name: str = Field(min_length=1)
    dataset: str = ''
    setup: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    results: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)

class VisualEvidence(BaseModel):
    id: str
    source_id: str
    title: str = Field(min_length=1)
    visual_type: Literal['figure','table','diagram','chart','image'] = 'image'
    description: str = ''
    claim_ids: list[str] = Field(default_factory=list)

class ClaimConflict(BaseModel):
    id: str
    kind: Literal['numeric_mismatch','polarity_mismatch']
    claim_ids: list[str] = Field(min_length=2, max_length=2)
    source_ids: list[str] = Field(min_length=1)
    field: str
    message: str

class ConceptEdge(BaseModel):
    source_id: str
    target_id: str
    relation: Literal['prerequisite','part_of','contrasts','related']
    reason: str = ''

class KnowledgeIR(BaseModel):
    schema_version: int = 1
    claims: list[Claim] = Field(default_factory=list)
    concepts: list[Concept] = Field(default_factory=list)
    formulas: list[Formula] = Field(default_factory=list)
    experiments: list[Experiment] = Field(default_factory=list)
    visuals: list[VisualEvidence] = Field(default_factory=list)
    conflicts: list[ClaimConflict] = Field(default_factory=list)
    concept_edges: list[ConceptEdge] = Field(default_factory=list)

class Chapter(BaseModel):
    id: str
    title: str
    card_ids: list[str] = Field(min_length=1)

class EditRequest(BaseModel):
    expected_version: int = Field(ge=1)
    markdown: str = Field(max_length=100000)

class ReviseRequest(BaseModel):
    expected_version: int = Field(ge=1)
    instruction: str = Field(min_length=1, max_length=4000)

class RestoreRequest(BaseModel):
    expected_version: int = Field(ge=1)
    version: int = Field(ge=1)
