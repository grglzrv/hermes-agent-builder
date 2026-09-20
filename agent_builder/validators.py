import re
from .errors import ValidationError
DISPLAY=re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{1,79}$")

def display_name(v):
    v=str(v).strip()
    if not DISPLAY.fullmatch(v): raise ValidationError("name must be 2-80 safe characters")
    return v

def slug(v):
    s=re.sub(r"[^a-z0-9]+","-",v.lower()).strip("-")[:45]
    if not s: raise ValidationError("name has no usable slug")
    return s

def text(v, limit=4000):
    v=str(v or "").strip()
    if len(v)>limit or chr(0) in v: raise ValidationError("text is invalid or too long")
    return v

def selections(values, valid, label):
    values=list(dict.fromkeys(values or []))
    bad=set(values)-set(valid)
    if bad: raise ValidationError(f"unknown {label}: "+", ".join(sorted(bad)))
    return values
