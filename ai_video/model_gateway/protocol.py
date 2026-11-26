"""Translate Responses message/tool/schema data to Chat Completions and back."""
import json


def json_value_contract(schema):
    """Describe result fields without presenting a JSON schema to be copied."""
    lines = []
    def visit(node, path, required):
        kind = node.get('type', 'any')
        kinds = kind if isinstance(kind, list) else [kind]
        details = [' or '.join(kinds), 'required' if required else 'optional']
        if 'enum' in node:
            details.append('allowed values=' + json.dumps(node['enum'], ensure_ascii=False))
        for key, label in (('minimum', 'minimum'), ('maximum', 'maximum'),
                           ('minItems', 'minimum entries'), ('maxItems', 'maximum entries'),
                           ('minLength', 'minimum length'), ('maxLength', 'maximum length')):
            if key in node:
                details.append(label + '=' + str(node[key]))
        if node.get('additionalProperties') is False:
            details.append('no extra fields')
        lines.append(path + ': ' + '; '.join(details))
        if 'object' in kinds:
            for key, child in node.get('properties', {}).items():
                visit(child, path + '.' + key, key in node.get('required', []))
        if 'array' in kinds and isinstance(node.get('items'), dict):
            visit(node['items'], path + '[]', True)
    visit(schema, '$', True)
    return '\n'.join(lines)


def content_parts(value):
    if isinstance(value, str):
        return value
    parts = []
    for item in value or []:
        kind = item.get('type')
        if kind in ('input_text','output_text','text'):
            parts.append({'type':'text','text':item.get('text','')})
        elif kind == 'input_image':
            image = {'url':item['image_url']}
            if item.get('detail'):
                image['detail'] = item['detail']
            parts.append({'type':'image_url','image_url':image})
        elif kind == 'refusal':
            parts.append({'type':'text','text':item.get('refusal','')})
        else:
            raise ValueError('선택한 AI의 호환 API에서 지원하지 않는 입력 형식입니다: ' + str(kind))
    return parts


def chat_payload(body, route):
    if body.get('previous_response_id'):
        raise ValueError('다른 AI 공급사에는 전체 대화 내용을 전달해야 합니다.')
    messages = []
    if body.get('instructions'):
        messages.append({'role':'system','content':body['instructions']})
    inputs = body.get('input', [])
    if isinstance(inputs, str):
        inputs = [{'role':'user','content':inputs}]
    for item in inputs:
        kind = item.get('type')
        if kind == 'reasoning':
            continue
        if kind == 'function_call':
            call = {'id':item['call_id'],'type':'function',
                    'function':{'name':item['name'],'arguments':item['arguments']}}
            if messages and messages[-1].get('role') == 'assistant' and messages[-1].get('tool_calls'):
                messages[-1]['tool_calls'].append(call)
            else:
                messages.append({'role':'assistant','content':None,'tool_calls':[call]})
        elif kind == 'function_call_output':
            output = item.get('output','')
            messages.append({'role':'tool','tool_call_id':item['call_id'],
                             'content':output if isinstance(output,str) else json.dumps(output,ensure_ascii=False)})
        elif item.get('role'):
            role = 'system' if item['role'] == 'developer' else item['role']
            messages.append({'role':role,'content':content_parts(item.get('content',''))})
        else:
            raise ValueError('호환 AI 요청에 알 수 없는 대화 항목이 있습니다.')
    payload = {'model':route.model,'messages':messages,'stream':False}
    limit = body.get('max_output_tokens') or route.options.get('max_tokens') or 8192
    token_key = 'max_completion_tokens' if route.options.get('use_completion_tokens') else 'max_tokens'
    payload[token_key] = limit
    tools = body.get('tools')
    if tools:
        payload['tools'] = []
        for tool in tools:
            if tool.get('type') != 'function':
                raise ValueError('선택한 AI 모델은 함수 도구만 지원합니다.')
            payload['tools'].append({'type':'function','function':{
                key:tool[key] for key in ('name','description','parameters','strict') if key in tool}})
    if 'tool_choice' in body:
        choice = body['tool_choice']
        payload['tool_choice'] = ({'type':'function','function':{'name':choice['name']}}
                                  if isinstance(choice,dict) and choice.get('type') == 'function' else choice)
    fmt = body.get('text',{}).get('format',{})
    if fmt.get('type') == 'json_schema':
        schema = {key:fmt[key] for key in ('name','schema','strict') if key in fmt}
        if route.options.get('schema_mode') == 'json_object' or route.provider in ('deepseek','moonshot','kimi'):
            contract = ('\nReturn a JSON INSTANCE containing actual result values for the task above. '
                        'Do not return the schema, field definitions, examples, or explanations. '
                        'Perform the requested analysis and fill each field with its actual result. '
                        'Boolean fields must contain true or false, not descriptions of boolean types. '
                        'For a required array with no findings, return an empty array. '
                        'Result field requirements (paths describe structure, not literal output keys):\n'
                        + json_value_contract(fmt['schema']))
            if route.options.get('schema_mode') == 'json_object' and messages and messages[0]['role'] == 'system':
                # Some local chat templates discard or confuse consecutive system messages.
                messages[0] = {**messages[0], 'content': messages[0]['content'] + contract}
            else:
                messages.insert(0, {'role': 'system', 'content': contract})
            payload['response_format'] = {'type':'json_object'}
        else:
            payload['response_format'] = {'type':'json_schema','json_schema':schema}
    for key, value in (route.options.get('extra_body') or {}).items():
        if key in ('model','messages','tools','response_format','stream'):
            raise ValueError('모델 매핑의 extra_body가 공통 요청 필드를 덮어쓸 수 없습니다.')
        payload[key] = value
    return payload


def response_payload(value):
    choice = value['choices'][0]
    message = choice['message']
    text = message.get('content') or ''
    if not isinstance(text,str):
        raise ValueError('Invalid model text')
    output = []
    if text:
        output.append({'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]})
    if message.get('refusal'):
        output.append({'type':'message','role':'assistant','content':[{'type':'refusal','refusal':message['refusal']}]})
    for item in message.get('tool_calls') or []:
        output.append({'type':'function_call','call_id':item['id'],
                       'name':item['function']['name'],'arguments':item['function']['arguments']})
    raw = value.get('usage') or {}
    usage = {'input_tokens':raw.get('prompt_tokens'), 'output_tokens':raw.get('completion_tokens'),
             'total_tokens':raw.get('total_tokens'), 'input_tokens_details':raw.get('prompt_tokens_details',{}),
             'output_tokens_details':raw.get('completion_tokens_details',{}), 'provider_usage':raw}
    completed = choice.get('finish_reason') in ('stop','tool_calls') and bool(output)
    result = {'id':value.get('id'),'status':'completed' if completed else 'incomplete',
              'output_text':text,'output':output,'usage':usage}
    if not completed:
        result['incomplete_details'] = {'reason':choice.get('finish_reason') or 'unknown'}
    return result
