TEMPLATE='''#include <nan.h>
#include <ccan/ccan/endian/endian.h>

#include "../include/wally_core.h"
#include "../include/wally_bip32.h"
#include "bip32_int.h"
#include "../include/wally_bip38.h"
#include "../include/wally_bip39.h"
#include "../include/wally_crypto.h"
#include "../include/wally_elements.h"

namespace {

typedef v8::Local<v8::Object> LocalObject;

template<typename T>
static bool IsValid(const typename v8::Local<T>& local)
{
    return !local.IsEmpty() && !local->IsNull() && !local->IsUndefined();
}

template<typename T>
static bool IsValid(const typename Nan::Maybe<T>& maybe)
{
    return maybe.IsJust();
}

// Binary data is expected as objects supporting the JS Buffer interface
struct LocalArray {
    LocalArray(Nan::NAN_METHOD_ARGS_TYPE info, int n, int& ret)
        : mData(0), mLength(0)
    {
        if (ret != WALLY_OK)
            return; // Do nothing, caller will already throw
        if (IsValid(info[n])) {
            if (!node::Buffer::HasInstance(info[n]))
                ret = WALLY_EINVAL;
            else {
                mBuffer = info[n]->ToObject();
                if (IsValid(mBuffer)) {
                    mData = (unsigned char*) node::Buffer::Data(mBuffer);
                    mLength = node::Buffer::Length(mBuffer);
                }
            }
        }
    }

    LocalArray(size_t len, int& ret)
        : mData(0), mLength(0)
    {
        if (ret != WALLY_OK)
            return; // Do nothing, caller will already throw
        const v8::MaybeLocal<v8::Object> local = Nan::NewBuffer(len);
        if (local.ToLocal(&mBuffer)) {
            mData = (unsigned char*) node::Buffer::Data(mBuffer);
            mLength = len;
        }
    }

    LocalObject mBuffer;
    unsigned char *mData;
    size_t mLength;
};

// uint32_t values are expected as normal JS numbers from 0 to 2^32-1
static uint32_t GetUInt32(Nan::NAN_METHOD_ARGS_TYPE info, int n, int& ret)
{
    uint32_t value = 0;
    if (ret == WALLY_OK) {
        if (!IsValid(info[n]) || !info[n]->IsUint32())
            ret = WALLY_EINVAL;
        else {
            Nan::Maybe<uint32_t> m = Nan::To<uint32_t>(info[n]);
            if (IsValid(m))
                value = m.FromJust();
            else
                ret = WALLY_EINVAL;
        }
    }
    return value;
}

// uint64_t values are expected as an 8 byte buffer of big endian bytes
struct LocalUInt64 : public LocalArray {
    LocalUInt64(Nan::NAN_METHOD_ARGS_TYPE info, int n, int& ret)
        : LocalArray(info, n, ret)
    {
        if (mData || mLength) {
            if (mLength != sizeof(mValue))
                ret = WALLY_EINVAL;
            else {
                memcpy(&mValue, mData, sizeof(mValue));
                mValue = be64_to_cpu(mValue);
            }
        } else if (ret == WALLY_OK)
            ret = WALLY_EINVAL; // Null not allowed for uint64_t values
    }
    uint64_t mValue;
};

static bool CheckException(Nan::NAN_METHOD_ARGS_TYPE info,
                           int ret, const char* errorText)
{
    switch (ret) {
    case WALLY_ERROR:
        Nan::ThrowError(errorText);
        return true;
    case WALLY_EINVAL:
        Nan::ThrowTypeError(errorText);
        return true;
    case WALLY_ENOMEM:
        Nan::ThrowError(errorText); // FIXME: Better Error?
        return true;
    }
    return false;
}

}

!!nan_impl!!

NAN_MODULE_INIT(Init) {
    !!nan_decl!!
}

NODE_MODULE(wallycore, Init)'''

def _generate_nan(funcname, f):
    input_args = []
    output_args = []
    args = []
    result_wrap = 'res'
    postprocessing = []
    num_outs = len([arg for arg in f.arguments if 'out' in arg])
    if num_outs > 1:
        cur_out = 0
        input_args.append('v8::Local<v8::Array> res = v8::Array::New(v8::Isolate::GetCurrent(), %s);' % num_outs)
    for i, arg in enumerate(f.arguments):
        if isinstance(arg, tuple):
            # Fixed output array size
            output_args.append('LocalArray res(%s, ret);' % arg[1])
            output_args.append('if (ret == WALLY_OK && !res.mLength) ret = WALLY_ENOMEM;')
            args.append('res.mData')
            args.append('res.mLength')
            result_wrap = 'res.mBuffer'
        elif arg.startswith('const_bytes'):
            input_args.append('LocalArray arg%s(info, %s, ret);' % (i, i))
            args.append('arg%s.mData' % i)
            args.append('arg%s.mLength' % i)
        elif arg.startswith('uint32_t'):
            input_args.append('uint32_t arg%s = GetUInt32(info, %s, ret);' % (i, i))
            args.append('arg%s' % i)
        elif arg.startswith('string'):
            args.append('*Nan::Utf8String(info[%s])' % i)
        elif arg.startswith('const_uint64s'):
            input_args.extend([
                'v8::Array *arr%s = (v8::Array*)*(info[%s]->ToObject());' % (i, i),
                'uint64_t *uint64s%s = new uint64_t[arr%s->Length()];' % (i, i),
                'for (size_t i = 0; i < arr%s->Length(); ++i) {' % i,
                '   unsigned char *bytes = (unsigned char*) node::Buffer::Data(arr%s->Get(i)->ToObject());' % i,
                '   uint64s%s[i] = be64_to_cpu(*((uint64_t*)bytes));' % i,
                '}'
            ])
            postprocessing.append('delete[] uint64s%s;' % i)
            args.append('uint64s%s' % i)
            args.append('arr%s->Length()' % i)
        elif arg.startswith('uint64_t'):
            input_args.append('LocalUInt64 arg%s(info, %s, ret);' % (i, i))
            args.append('arg%s.mValue' % i)
        elif arg == 'out_str_p':
            output_args.append('char *result_ptr;')
            args.append('&result_ptr')
            result_wrap = 'v8::String::NewFromUtf8(v8::Isolate::GetCurrent(), result_ptr)'
        elif arg == 'out_bytes_sized':
            output_args.extend([
                'const size_t res_size = info[%s]->ToInteger()->Value();' % i,
                'unsigned char *res_ptr = new unsigned char[res_size];',
                'size_t out_size;'
            ])
            args.append('res_ptr')
            args.append('res_size')
            args.append('&out_size')
            postprocessing.append('LocalObject res = Nan::NewBuffer((char*)res_ptr, out_size).ToLocalChecked();')
        elif arg == 'out_bytes_fixedsized':
            output_args.extend([
                'const size_t res_size%s = info[%s]->ToInteger()->Value();' % (i, i),
                'LocalObject res%s = Nan::NewBuffer(res_size%s).ToLocalChecked();' % (i, i),
                'unsigned char *res_ptr%s  = (unsigned char*) node::Buffer::Data(res%s);' % (i, i)
            ])
            args.append('res_ptr%s' % i)
            args.append('res_size%s' % i)
            if num_outs > 1:
                postprocessing.append('res->Set(%s, res%s);' % (cur_out, i))
                cur_out += 1
            else:
                output_args.append(
                    'LocalObject res = res%s;' % i,
                )
        elif arg == 'out_uint64_t':
            assert num_outs > 1  # wally_asset_unblind is the only func using this type
            output_args.extend([
                'uint64_t *res%s = (uint64_t*)malloc(8);' % i,
            ])
            args.append('res%s' % i)
            postprocessing.extend([
                '*res%s = cpu_to_be64(*res%s);' % (i, i),
                'res->Set(%s, Nan::NewBuffer((char*)res%s, 8).ToLocalChecked());' % (cur_out, i)
            ])
            cur_out += 1
        elif arg == 'bip32_in':
            input_args.append((
                'const ext_key* inkey;'
                'unsigned char* inbuf = (unsigned char*) node::Buffer::Data(info[%s]->ToObject());'
                'bip32_key_unserialize_alloc(inbuf, node::Buffer::Length(info[%s]->ToObject()), &inkey);'
            ) % (i, i))
            args.append('inkey');
            postprocessing.append('bip32_key_free(inkey);')
        elif arg in ['bip32_pub_out', 'bip32_priv_out']:
            output_args.append(
                'const ext_key *outkey;'
                'LocalObject res = Nan::NewBuffer(BIP32_SERIALIZED_LEN).ToLocalChecked();'
                'unsigned char *out = (unsigned char*) node::Buffer::Data(res);'
            )
            args.append('&outkey')
            flag = {'bip32_pub_out': 'BIP32_FLAG_KEY_PUBLIC',
                    'bip32_priv_out': 'BIP32_FLAG_KEY_PRIVATE'}[arg]
            postprocessing.append('bip32_key_serialize(outkey, %s, out, BIP32_SERIALIZED_LEN);' % flag)
            postprocessing.append('bip32_key_free(outkey);')
        else:
            assert False, 'unknown argument type'

    call_name = (f.wally_name or funcname) + ('_alloc' if f.nodejs_append_alloc else '')
    return ('''
NAN_METHOD(%s) {
    int ret = WALLY_OK;
    !!input_args!!
    !!output_args!!
    if (ret == WALLY_OK)
        ret = %s(!!args!!);
    !!postprocessing!!
    if (!CheckException(info, ret, "%s"))
        info.GetReturnValue().Set(%s);
}
''' % (funcname, call_name, funcname, result_wrap)).replace(
        '!!input_args!!', '\n    '.join(input_args)
    ).replace(
        '!!output_args!!', '\n    '.join(output_args)
    ).replace(
        '!!args!!', ', '.join(args)
    ).replace(
        '!!postprocessing!!', '\n    '.join(postprocessing)
    )

def generate(functions, build_type):
    nan_implementations = []
    nan_declarations = []
    for i, (funcname, f) in enumerate(functions):
        nan_implementations.append(_generate_nan(funcname, f))
        nan_declarations.append('NAN_EXPORT(target, %s);' % funcname)
    return TEMPLATE.replace(
        '!!nan_impl!!',
        ''.join(nan_implementations)
    ).replace(
        '!!nan_decl!!',
        '\n    '.join(nan_declarations)
    )