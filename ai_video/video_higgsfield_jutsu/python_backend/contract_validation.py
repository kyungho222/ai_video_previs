"""Shared, non-mutating validation for the JSON contract subset used by this service."""
import math


def contract_issues(value, schema, path='', *, allow_extra=False):
    issues = []
    def visit(v, spec, where):
        def fail(message):
            issues.append({'path':where, 'message':message})
        expected = spec.get('type')
        types = expected if isinstance(expected,list) else [expected] if expected else []
        finite = type(v) in (int,float) and math.isfinite(v)
        matches = {'object':isinstance(v,dict),'array':isinstance(v,list),
                   'string':isinstance(v,str),'number':finite,'integer':type(v) is int,
                   'boolean':type(v) is bool,'null':v is None}
        if types and not any(matches.get(t,False) for t in types):
            fail('expected ' + '/'.join(types))
            return
        if 'enum' in spec and not any(type(v) is type(item) and v == item for item in spec['enum']):
            fail('unsupported value')
        if isinstance(v,dict):
            properties=spec.get('properties',{})
            for key in spec.get('required',[]):
                if key not in v:
                    issues.append({'path':where+'.'+key if where else key, 'message':'required field missing'})
            for key,child in properties.items():
                if key in v:
                    visit(v[key],child,where+'.'+key if where else key)
            if not allow_extra and spec.get('additionalProperties') is False:
                for key in sorted(v.keys()-properties.keys(),key=str):
                    issues.append({'path':where+'.'+str(key) if where else str(key),'message':'unsupported field'})
        elif isinstance(v,list):
            if not spec.get('minItems',0) <= len(v) <= spec.get('maxItems',math.inf):
                fail('array length outside allowed bounds')
            if isinstance(spec.get('items'),dict):
                for i,item in enumerate(v):
                    visit(item,spec['items'],f'{where}[{i}]')
        elif finite:
            if not spec.get('minimum',-math.inf) <= v <= spec.get('maximum',math.inf):
                fail('number outside allowed bounds')
        elif isinstance(v,str):
            if not spec.get('minLength',0) <= len(v) <= spec.get('maxLength',math.inf):
                fail('text length outside allowed bounds')
    visit(value,schema,path)
    return issues
