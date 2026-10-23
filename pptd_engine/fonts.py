"""Shared font resolution and editable PowerPoint EOT embedding.

EOT v2.2 follows https://www.w3.org/submissions/EOT/ and LibreOffice's
EOTConverter. Full sfnt data is retained (no glyph subsetting or Word XOR).
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import hashlib
import io
import os
import posixpath
import re
import struct
import subprocess
import sys
import tempfile
from urllib.parse import urlparse
from zipfile import ZipFile, ZIP_DEFLATED
import requests
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from lxml import etree

P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
# Families that cannot be installed on the server: proprietary Windows/Office
# faces and platform defaults. They map onto the open equivalent that is
# actually present so a company template keeps its typography intent instead of
# failing the export. Every substitution is reported by the exporter, never
# applied silently. 思源黑体 is the open release of 微软雅黑.
ALIASES = {
    '思源黑体': 'Noto Sans CJK SC',
    '微软雅黑': 'Noto Sans CJK SC',
    '微软雅黑 Light': 'Noto Sans CJK SC',
    '黑体': 'Noto Sans CJK SC',
    '等线': 'Noto Sans CJK SC',
    '等线 Light': 'Noto Sans CJK SC',
    '楷体': 'Noto Sans CJK SC',
    '仿宋': 'Noto Sans CJK SC',
    '宋体': 'Noto Serif CJK SC',
    '新宋体': 'Noto Serif CJK SC',
    'Noto Sans SC': 'Noto Sans CJK SC',
    'Arial': 'Liberation Sans',
    'Arial Bold': 'Liberation Sans',
    'Helvetica': 'Liberation Sans',
    'Helvetica Neue': 'Liberation Sans',
    'Calibri': 'Liberation Sans',
    'Calibri Light': 'Liberation Sans',
    'Times New Roman': 'Liberation Serif',
}
# The open CJK faces ship under different family names per platform, and the
# proprietary Windows faces ALIASES substitutes away are actually installed
# there. Both substitutions are therefore platform specific; POSIX keeps the
# ALIASES table above unchanged.
WINDOWS_FAMILIES = {
    'Noto Sans CJK SC': 'Noto Sans SC',
    'Noto Serif CJK SC': 'Noto Serif SC',
    'Liberation Sans': 'Arial',
    'Liberation Serif': 'Times New Roman',
}
STIX_MATH_URL = 'https://raw.githubusercontent.com/google/fonts/main/ofl/stixtwomath/STIXTwoMath-Regular.ttf'
FONT_SLOT_ORDER = {'font': 0, 'regular': 1, 'bold': 2, 'italic': 3, 'boldItalic': 4}
GOOGLE_UA = 'Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/36.0.1985.125 Safari/537.36'


def canonical_family(family):
    """The family name the exporter embeds on this platform."""
    canonical = ALIASES.get(family, family)
    if sys.platform == 'win32':
        canonical = WINDOWS_FAMILIES.get(canonical, canonical)
    return canonical


def substitution_note(source: str, target: str) -> str:
    """Explain a font substitution in the exporter's user-readable report."""
    if sys.platform == 'win32':
        # The proprietary face is installed here but is still substituted onto its
        # open equivalent, so the note cannot claim that it was missing.
        return f'字体替换：{source} 已改用开源等价字体 {target} 并随文件内嵌。'
    return f'字体替换：{source} 未安装，已改用开源等价字体 {target} 并随文件内嵌。'


def editable_permission(font):
    flags = font['OS/2'].fsType
    if flags & 0x200:
        raise ValueError('Bitmap-only embedding permission forbids outline embedding')
    # Legacy fonts can set multiple bits; the least restrictive permission wins.
    if flags & 0xE and not flags & 8:
        raise ValueError(f'Font does not permit editable embedding (fsType={flags})')


def _name(font, name_id):
    names = font['name'].names
    preferred = sorted((n for n in names if n.nameID == name_id), key=lambda n: (n.langID != 0x409, n.platformID != 3))
    return preferred[0].toUnicode() if preferred else ''


def eot_encode(sfnt: bytes) -> bytes:
    font = TTFont(io.BytesIO(sfnt)); editable_permission(font)
    os2 = font['OS/2']
    panose = bytes(getattr(os2.panose, k) for k in ('bFamilyType','bSerifStyle','bWeight','bProportion','bContrast','bStrokeVariation','bArmStyle','bLetterForm','bMidline','bXHeight'))
    header = struct.pack('<4I',0,len(sfnt),0x20002,0) + panose + bytes((0,os2.fsSelection & 1))
    header += struct.pack('<IHH',os2.usWeightClass,os2.fsType,0x504c)
    header += struct.pack('<7I',*(getattr(os2,f'ulUnicodeRange{i}',0) for i in range(1,5)),*(getattr(os2,f'ulCodePageRange{i}',0) for i in range(1,3)),font['head'].checkSumAdjustment)
    header += bytes(16)
    for name_id in (1,2,5,4):
        value = (_name(font,name_id)+'\0').encode('utf-16le')
        header += struct.pack('<HH',0,len(value))+value
    header += struct.pack('<HHIIHHII',0,0,0x50475342,1252,0,0,0,0)
    payload = header + sfnt
    return struct.pack('<I',len(payload))+payload[4:]


def eot_decode(data: bytes) -> bytes:
    if len(data)<108: raise ValueError('Truncated EOT')
    total,size,version,flags = struct.unpack_from('<4I',data)
    if total != len(data) or version != 0x20002 or flags != 0 or struct.unpack_from('<H',data,34)[0] != 0x504c:
        raise ValueError('Invalid/unhandled EOT header')
    offset=80
    for _ in range(4):
        padding,length=struct.unpack_from('<HH',data,offset)
        if padding or length%2: raise ValueError('Invalid EOT name')
        offset+=4+length
    if offset+24+size != len(data): raise ValueError('Invalid EOT data size')
    raw=data[offset+24:]
    TTFont(io.BytesIO(raw))
    return raw


@dataclass(frozen=True)
class FontAsset:
    family: str
    path: Path
    weight: int = 400
    style: str = 'normal'
    source: str = 'system'
    synthesis: str | None = None

    @property
    def slot(self):
        return ('boldItalic' if self.style=='italic' else 'bold') if self.weight>=600 else ('italic' if self.style=='italic' else 'regular')

    def check_coverage(self,text):
        cmap=TTFont(self.path).getBestCmap()
        missing=sorted({c for c in text if not c.isspace() and ord(c)>=32 and ord(c) not in cmap})
        if missing: raise ValueError(f'{self.family} missing glyphs: '+''.join(missing[:30]))

    def public(self):
        return {'family':self.family,'url':'/fonts/'+self.path.name,'weight':self.weight,'style':self.style,'source':self.source,'synthesis':self.synthesis}


def _family_names(font):
    return {n.toUnicode().casefold() for n in font['name'].names if n.nameID in (1,16)}


def _face_score(font, weight, italic):
    """Rank a face by how well it supplies the requested weight and slant."""
    axes = {axis.axisTag:axis for axis in font['fvar'].axes} if 'fvar' in font else {}
    variable_weight = 'wght' in axes and axes['wght'].minValue <= weight <= axes['wght'].maxValue
    variable_style = ('ital' in axes and axes['ital'].minValue <= int(italic) <= axes['ital'].maxValue) or ('slnt' in axes and (not italic or axes['slnt'].minValue != axes['slnt'].maxValue))
    actual_weight = font['OS/2'].usWeightClass
    actual_italic = bool(font['OS/2'].fsSelection & 1)
    return (0 if variable_style or actual_italic == italic else 10000) + (0 if variable_weight else abs(actual_weight - weight))


def _select_collection_face(raw, family, weight, italic):
    """A custom TTC URL identifies a collection, not necessarily its first face."""
    if raw[:4] != b'ttcf':
        return 0
    count = struct.unpack_from('>I', raw, 8)[0]
    candidates = []
    for index in range(count):
        font = TTFont(io.BytesIO(raw), fontNumber=index, lazy=True)
        try:
            if family.casefold() not in _family_names(font):
                continue
            score = _face_score(font, weight, italic)
            candidates.append((score, index))
        finally:
            font.close()
    if not candidates:
        raise ValueError(f'Font family mismatch for {family}: no matching TTC face')
    return min(candidates)[1]


def _instance_face(font, weight, italic):
    if 'fvar' not in font:
        return font
    axes = {axis.axisTag:axis for axis in font['fvar'].axes}
    coords = {tag:axis.defaultValue for tag,axis in axes.items()}
    if 'wght' in axes:
        axis = axes['wght']
        if not axis.minValue <= weight <= axis.maxValue:
            raise ValueError(f'Requested font weight {weight} is outside variable axis range {axis.minValue}..{axis.maxValue}')
        coords['wght'] = weight
    style_axis = False
    if 'ital' in axes:
        axis = axes['ital']
        if not axis.minValue <= int(italic) <= axis.maxValue:
            raise ValueError('Variable ital axis cannot supply the requested face')
        coords['ital'] = int(italic)
        style_axis = True
    if 'slnt' in axes:
        axis = axes['slnt']
        # Prefer the conventional 12-degree oblique angle, within the font's range.
        slant = 0 if not italic or 'ital' in axes else (max(axis.minValue,-12) if axis.minValue < 0 else min(axis.maxValue,12))
        if not axis.minValue <= slant <= axis.maxValue or (italic and 'ital' not in axes and slant == 0):
            raise ValueError('Variable slnt axis cannot supply the requested face')
        coords['slnt'] = slant
        style_axis = True
    font = instantiateVariableFont(font, coords, inplace=True, downgradeCFF2="CFF2" in font)
    if style_axis:
        # Instancer changes outlines and removes variation tables, but by default
        # retains the original Regular name/italic bit. Set the static face flags.
        selection = font['OS/2'].fsSelection & ~(1 | 32 | 64)
        bold = font['OS/2'].usWeightClass >= 600
        font['OS/2'].fsSelection = selection | (1 if italic else 0) | (32 if bold else 0) | (64 if not italic and not bold else 0)
        font['head'].macStyle = (font['head'].macStyle & ~3) | (1 if bold else 0) | (2 if italic else 0)
        style = 'Bold Italic' if bold and italic else 'Bold' if bold else 'Italic' if italic else 'Regular'
        for record in list(font['name'].names):
            if record.nameID in (2,17):
                font['name'].setName(style,record.nameID,record.platformID,record.platEncID,record.langID)
        if 'post' in font and 'slnt' in coords:
            font['post'].italicAngle = coords['slnt']
    return font


def _windows_font_directories():
    """Directories that hold installed Windows font files."""
    directories = []
    windir = os.environ.get('WINDIR')
    if windir:
        directories.append(Path(windir) / 'Fonts')
    local = os.environ.get('LOCALAPPDATA')
    if local:
        directories.append(Path(local) / 'Microsoft' / 'Windows' / 'Fonts')
    return directories


def _windows_installed_fonts(family):
    """Registry file names of the installed fonts that may provide the family."""
    import winreg

    wanted = family.casefold()
    filenames = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(root, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts')
        except OSError:
            continue
        with key:
            for index in range(winreg.QueryInfoKey(key)[1]):
                name, value, _ = winreg.EnumValue(key, index)
                if wanted in name.casefold():
                    filenames.append(value)
    return filenames


def _windows_font_match(family, weight, italic):
    """The fc-match equivalent on Windows: family list, file and face index."""
    candidates = []
    seen = set()
    for filename in _windows_installed_fonts(family):
        path = None
        for directory in _windows_font_directories():
            candidate = directory / filename
            if candidate.is_file():
                path = candidate
                break
        if path is None or str(path).casefold() in seen:
            continue
        seen.add(str(path).casefold())
        raw = path.read_bytes()
        try:
            index = _select_collection_face(raw, family, weight, italic)
        except ValueError:
            continue
        font = TTFont(io.BytesIO(raw), fontNumber=index, lazy=True)
        try:
            names = _family_names(font)
            if family.casefold() not in names:
                continue
            candidates.append((_face_score(font, weight, italic), str(path), index, names))
        finally:
            font.close()
    if not candidates:
        return []
    _, path, index, names = min(candidates, key=lambda candidate: (candidate[0], candidate[1]))
    return [', '.join(sorted(names)), path, str(index)]


class FontResolver:
    def __init__(self,cache: Path,custom_fonts=()):
        self.cache=Path(cache); self.cache.mkdir(parents=True,exist_ok=True)
        self.custom={'MiSans':'https://statics.kimi.ai/neo-design-static/p-font/MiSans.ttf', 'STIX Two Math':STIX_MATH_URL, **{f['family']:f['src'] for f in custom_fonts}}
        self._resolved={}

    def _download(self,url):
        if urlparse(url).scheme!='https': raise ValueError('Font source must use HTTPS')
        cache=self.cache/(hashlib.sha256(url.encode()).hexdigest()+'.download')
        if cache.exists(): return cache.read_bytes()
        response=requests.get(url,headers={'User-Agent':GOOGLE_UA},timeout=30); response.raise_for_status()
        if len(response.content)>80_000_000: raise ValueError('Font download exceeds 80 MB')
        cache.write_bytes(response.content)
        return response.content

    def resolve(self,family,weight=400,italic=False):
        family=canonical_family(family)
        key=(family,weight,italic)
        if key in self._resolved:return self._resolved[key]
        source=self.custom.get(family)
        if source:
            raw=self._download(source)
            if urlparse(source).hostname=='fonts.googleapis.com':
                css=raw.decode(); matches=re.findall(r'@font-face\s*\{([^}]+)\}',css)
                if len(matches)!=1: raise ValueError('Expected full default font from Google CSS; subset or multi-face response is not supported')
                found=re.search(r'url\([\"\']?([^\)\"\']+)',matches[0])
                if not found: raise ValueError('Google CSS has no font URL')
                raw=self._download(found.group(1))
            index=_select_collection_face(raw,family,weight,italic)
        else:
            pattern=family+(':style=Bold Italic' if weight>=600 and italic else ':style=Bold' if weight>=600 else ':style=Italic' if italic else ':style=Regular')
            if sys.platform=='win32':
                result=_windows_font_match(family,weight,italic)
            else:
                result=subprocess.run(['fc-match','-f','%{family}\n%{file}\n%{index}',pattern],capture_output=True,text=True,check=True).stdout.splitlines()
            if not result or family.casefold() not in [f.strip().casefold() for f in result[0].split(',')]:
                raise ValueError(f'Font {family!r} is not installed; declare customFonts with an explicit source')
            raw=Path(result[1]).read_bytes(); index=int(result[2] or 0)&0xffff; source='system:'+result[1]
        font=TTFont(io.BytesIO(raw),fontNumber=index,recalcTimestamp=False)
        names=_family_names(font)
        if family.casefold() not in names: raise ValueError(f'Font family mismatch for {family}: {sorted(names)}')
        font=_instance_face(font,weight,italic)
        if family == 'STIX Two Math' and 'MATH' not in font:
            raise ValueError('STIX Two Math resource is missing the OpenType MATH layout table')
        actual_weight=font['OS/2'].usWeightClass
        actual_italic=bool(font['OS/2'].fsSelection&1)
        # customFonts declares the regular resource; unavailable variants are explicit.
        synthesis = None
        if (weight>=600)!=(actual_weight>=600) or italic!=actual_italic:
            synthesis = f'{family}: requested {weight}/{"italic" if italic else "normal"}; embed actual {actual_weight}/{"italic" if actual_italic else "normal"} face, rendering engine synthesizes style'
        editable_permission(font)
        font.recalcTimestamp=False; font.flavor=None; buffer=io.BytesIO();font.save(buffer);sfnt=buffer.getvalue()
        dest=self.cache/(hashlib.sha256(sfnt).hexdigest()+('.otf' if sfnt[:4]==b'OTTO' else '.ttf'))
        if not dest.exists():dest.write_bytes(sfnt)
        asset=FontAsset(family,dest,700 if actual_weight>=600 else 400,'italic' if actual_italic else 'normal',source,synthesis)
        self._resolved[key]=asset
        return asset


def embed_fonts(path: Path, assets) -> list[dict]:
    with ZipFile(path) as z: entries={name:z.read(name) for name in z.namelist()}
    root=etree.fromstring(entries['ppt/presentation.xml'])
    rels=etree.fromstring(entries['ppt/_rels/presentation.xml.rels'])
    ct=etree.fromstring(entries['[Content_Types].xml'])
    lst=root.find(f'{{{P}}}embeddedFontLst')
    if lst is None:
        lst=etree.Element(f'{{{P}}}embeddedFontLst')
        # CT_Presentation order: after notesSz/smartTags, before custShowLst/defaultTextStyle.
        before=next((x for x in root if etree.QName(x).localname in ('custShowLst','photoAlbum','custDataLst','kinsoku','defaultTextStyle','modifyVerifier','extLst')),None)
        if before is None:root.append(lst)
        else:root.insert(root.index(before),lst)
    root.set('embedTrueTypeFonts','1');root.set('saveSubsetFonts','0')
    if not any(x.get('Extension')=='fntdata' for x in ct):
        etree.SubElement(ct,f'{{{CT}}}Default',Extension='fntdata',ContentType='application/x-fontdata')
    used={r.get('Id') for r in rels};seen=set();audit=[]
    for asset in assets:
        if (asset.family,asset.slot) in seen:continue
        seen.add((asset.family,asset.slot))
        sfnt=asset.path.read_bytes(); payload=eot_encode(sfnt)
        part='fonts/'+hashlib.sha256((asset.family+asset.slot).encode()).hexdigest()[:20]+'.fntdata'
        entry=next((f for f in lst if f.find(f'{{{P}}}font') is not None and f.find(f'{{{P}}}font').get('typeface')==asset.family),None)
        if entry is None:
            entry=etree.SubElement(lst,f'{{{P}}}embeddedFont');etree.SubElement(entry,f'{{{P}}}font',typeface=asset.family)
        old_slots=list(entry.findall(f'{{{P}}}{asset.slot}'))
        old_rids={slot.get(f'{{{R}}}id') for slot in old_slots}
        for slot in old_slots:entry.remove(slot)
        # Replacing a face also retires its old relationships and parts when no
        # other embedded face still refers to them. Unrelated fonts stay intact.
        referenced={node.get(f'{{{R}}}id') for node in lst.iter() if node.get(f'{{{R}}}id')}
        retired_targets=[]
        for relationship in list(rels):
            if relationship.get('Id') in old_rids and relationship.get('Id') not in referenced:
                retired_targets.append(relationship.get('Target'))
                used.discard(relationship.get('Id'));rels.remove(relationship)
        remaining_targets={r.get('Target') for r in rels}
        for target in retired_targets:
            if target not in remaining_targets:
                retired=target.lstrip('/') if target.startswith('/') else posixpath.normpath('ppt/'+target)
                entries.pop(retired,None)
                for override in list(ct):
                    if override.get('PartName')=='/'+retired:ct.remove(override)
        rid='rIdPptdFont'+str(len(audit)+1)
        while rid in used:rid+='x'
        used.add(rid)
        etree.SubElement(rels,f'{{{REL}}}Relationship',Id=rid,Type=R+'/font',Target=part)
        etree.SubElement(entry,f'{{{P}}}{asset.slot}',{f'{{{R}}}id':rid})
        entry[:]=sorted(entry,key=lambda child:FONT_SLOT_ORDER.get(etree.QName(child).localname,99))
        entries['ppt/'+part]=payload
        audit.append({'family':asset.family,'style':asset.slot,'format':'EOT-uncompressed','part':'ppt/'+part,'bytes':len(payload),'sfntSha256':hashlib.sha256(sfnt).hexdigest(),'fullFont':True})
    for name,node in [('ppt/presentation.xml',root),('ppt/_rels/presentation.xml.rels',rels),('[Content_Types].xml',ct)]:
        entries[name]=etree.tostring(node,encoding='UTF-8',xml_declaration=True,standalone=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,suffix='.pptx',delete=False) as f:tmp=Path(f.name)
    try:
        with ZipFile(tmp,'w',ZIP_DEFLATED) as z:
            for name,raw in entries.items():z.writestr(name,raw)
        tmp.replace(path)
    finally:tmp.unlink(missing_ok=True)
    return audit



def is_east_asian(char):
    value=ord(char)
    return any(low <= value <= high for low,high in ((0x1100,0x11FF),(0x2E80,0x303F),(0x3040,0x31FF),(0x3400,0x4DBF),(0x4E00,0x9FFF),(0xA960,0xA97F),(0xAC00,0xD7FF),(0xF900,0xFAFF),(0xFF01,0xFF60),(0xFFE0,0xFFE6),(0x20000,0x323AF)))


def document_font_usage(document, pages):
    """Actual text glyphs by resolved face; formulas use their own math fonts."""
    from html.parser import HTMLParser
    from .math import split_math
    uses={}
    theme=document.get('theme',{})
    text_styles=theme.get('textStyles',{})
    def record(style,text=''):
        names=style.get('fontFamily','MiSans')
        split=isinstance(names,dict)
        names=names if split else {'all':names}
        for script,name in names.items():
            key=(ALIASES.get(name,name),700 if style.get('bold') else 400,bool(style.get('italic')))
            chars={c for c in text if not split or is_east_asian(c)==(script=='ea')}
            uses.setdefault(key,set()).update(chars)
    class Inline(HTMLParser):
        def __init__(self,style):super().__init__();self.stack=[('',style)]
        def handle_starttag(self,tag,attrs):
            style=dict(self.stack[-1][1]);attrs=dict(attrs)
            if tag=='strong':style['bold']=True
            if tag=='em':style['italic']=True
            for item in attrs.get('style','').split(';'):
                key,sep,value=item.partition(':')
                if sep and key.strip()=='font-family':style['fontFamily']=value.strip().strip("\"'")
            self.stack.append((tag,style))
        def handle_endtag(self,tag):
            for i in range(len(self.stack)-1,0,-1):
                if self.stack[i][0]==tag:self.stack=self.stack[:i];break
        def handle_data(self,data):
            for math,text in split_math(data):
                if not math:record(self.stack[-1][1],text)
    def content(value,inherited=None):
        reference=value.get('textStyle',value.get('style',''))
        base={**(inherited or {}),**text_styles.get(reference.lstrip('$'),{}),**value}
        record(base)
        Inline(base).feed(str(value.get('text','')))
    for page in pages:
        for element in page.get('elements',[]):
            kind=element.get('elementType')
            if kind=='text':content(element['content'])
            elif kind=='table':
                config=element.get('style',{})
                if isinstance(config,str):config=theme.get('tableStyles',{}).get(config.lstrip('$'),{})
                used=set();lastrow=len(element['rowHeights'])-1;lastcol=len(element['columnWidths'])-1
                for ri,row in enumerate(element['rows']):
                    ci=0
                    for cell in row:
                        while (ri,ci) in used:ci+=1
                        base=dict(config.get('cellStyle',{}));body=config.get('bodyStyles',[])
                        if 0<ri<lastrow and body:base.update(body[(ri-1)%len(body)])
                        r={**(config.get('firstRowStyle',{}) if ri==0 else {}),**(config.get('lastRowStyle',{}) if ri==lastrow else {})}
                        c={**(config.get('firstColumnStyle',{}) if ci==0 else {}),**(config.get('lastColumnStyle',{}) if ci==lastcol else {})}
                        for layer in ([c,r] if config.get('rowOverColumn',True) else [r,c]):base.update(layer)
                        content(cell,base)
                        rs,cs=cell.get('rowSpan',1),cell.get('colSpan',1)
                        for r in range(ri,ri+rs):
                            for c in range(ci,ci+cs):used.add((r,c))
                        ci+=cs
            elif kind=='chart':
                base={'fontFamily':element.get('fontFamily',theme.get('fontFamily','MiSans'))}
                values=' '.join(str(v) for row in element.get('data',{}).get('rows',[]) for v in row if v is not None)
                values+=' '.join(s.get('name','') for s in element.get('series',[]))
                record(base,values)
                def styles(value,parent):
                    if isinstance(value,dict):
                        style={**parent,**{k:v for k,v in value.items() if k in ('fontFamily','bold','italic')}}
                        record(style,values)
                        if isinstance(value.get('text'),str):content(value,style)
                        for item in value.values():styles(item,style)
                    elif isinstance(value,list):
                        for item in value:styles(item,parent)
                styles(element,base)
                if isinstance(element.get('title'),str):content({'text':element['title']},base)
    for custom in document.get('customFonts',[]):record({'fontFamily':custom['family']})
    return uses


def document_font_requests(document,pages):
    return sorted(document_font_usage(document,pages))
