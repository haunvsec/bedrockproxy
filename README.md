# Bedrock → OpenAI-Compatible Lambda Proxy

Hướng dẫn triển khai hoàn toàn bằng giao diện AWS Console: [AWS_CONSOLE_DEPLOYMENT.md](/Volumes/DATA/openai_bedrock/AWS_CONSOLE_DEPLOYMENT.md).

Lambda này expose một API gần giống OpenAI:

- `GET /v1/models`
- `GET /v1/quota`
- `POST /v1/chat/completions`

Để tương thích với code proxy ban đầu và các client tự nối endpoint, Lambda cũng nhận `GET /models`, `GET /quota` và `POST /chat/completions`. Vì vậy client có thể dùng Base URL gốc hoặc Base URL kết thúc bằng `/v1`.

Phía sau Lambda gọi Amazon Bedrock `Converse API`, rồi convert response về format OpenAI Chat Completions. Proxy cũng có quota theo số tiền/tháng, lưu usage, cấu hình và credential đã hash trong DynamoDB.

Hiện bản này hỗ trợ non-streaming chat completion. `stream=true`, tool calling, vision input chưa bật.

## 1. Tạo DynamoDB table để lưu quota

Tạo table:

- Table name: `BedrockOpenAIProxyQuota`
- Partition key: `quota_id` dạng `String`
- Billing mode: On-demand

Các item chính gồm `auth#credentials`, `config#proxy`, một item usage cho mỗi tháng dạng `global#YYYY-MM`, và log từng request dạng `request#YYYY-MM#TIMESTAMP#ID`. DynamoDB không cần khai báo trước các cột ngoài partition key; các item tháng cũ và request cũ chính là dữ liệu lịch sử, không cần table hoặc index mới.

## 2. Tạo Lambda function

Runtime:

- Python 3.12 hoặc Python 3.11
- Architecture: `arm64` hoặc `x86_64`
- Timeout Lambda: nên để `300s` (5 phút)
- Memory: `512 MB` là đủ cho proxy nhẹ

Khi triển khai hoàn toàn bằng AWS Console, dùng code editor của Lambda để thay nội dung `lambda_function.py`, sau đó tạo thêm file `dashboard.html` và dán nội dung tương ứng. Không cần cài dependency ngoài; runtime Lambda đã có `boto3`.

Handler:

```text
lambda_function.lambda_handler
```

## 3. Environment variables và khởi tạo credential

Lần triển khai đầu tiên cấu hình bốn secret:

```text
API_KEY=thay-bang-mot-secret-dai-ngau-nhien
ADMIN_PASSWORD=thay-bang-mat-khau-admin-manh
ADMIN_SESSION_SECRET=chuoi-ngau-nhien-toi-thieu-32-ky-tu
CREDENTIAL_HASH_KEY=chuoi-ngau-nhien-khac-toi-thieu-32-ky-tu
```

`API_KEY` phải dài ít nhất 24 ký tự, `ADMIN_PASSWORD` ít nhất 12 ký tự. Hai giá trị này chỉ dùng để bootstrap item `auth#credentials` ở lần gọi Lambda đầu tiên. Sau khi đăng nhập dashboard thành công và thấy item trên DynamoDB, xóa `API_KEY` và `ADMIN_PASSWORD` khỏi Lambda env. Trạng thái ổn định chỉ cần:

```text
ADMIN_SESSION_SECRET=chuoi-ngau-nhien-toi-thieu-32-ky-tu
CREDENTIAL_HASH_KEY=chuoi-ngau-nhien-khac-toi-thieu-32-ky-tu
```

Không đổi hoặc làm mất `CREDENTIAL_HASH_KEY`, vì mọi API key/password đang lưu sẽ không xác minh được. `ADMIN_SESSION_SECRET` dùng ký session; thay giá trị này sẽ đăng xuất tất cả admin. Hai key phải là chuỗi ngẫu nhiên khác nhau và tối thiểu 32 byte.

Proxy chấp nhận API key qua `Authorization: Bearer <key>` hoặc `x-api-key: <key>`. Username khởi tạo là `admin`; sau đó username, password và API key đều quản lý trên dashboard.

API key được lưu bằng HMAC-SHA256 có domain separation. Password được lưu bằng PBKDF2-HMAC-SHA256 210.000 vòng, salt ngẫu nhiên và pepper từ `CREDENTIAL_HASH_KEY`. Đây là hash một chiều có khóa, không phải mã hóa hai chiều, nên an toàn hơn vì Lambda không có chức năng đọc lại plaintext. API key mới chỉ hiển thị một lần khi tạo.

Các cấu hình không nhạy cảm nằm ở đầu [lambda_function.py](/Volumes/DATA/openai_bedrock/lambda_function.py): region Singapore, read timeout 240 giây, table name, global quota, model map, giá token và giá trị mặc định. Ngân sách tháng, giới hạn token và trạng thái bật/tắt model chỉnh trực tiếp trên dashboard rồi lưu vào DynamoDB.

Model mặc định và mức giá USD trên 1 triệu token đang cấu hình:

| Model Identifier dùng với proxy | Bedrock Global Inference ID | Input | Output |
|---|---|---:|---:|
| `amazon-nova-lite` | `global.amazon.nova-2-lite-v1:0` | `$0.30` | `$2.50` |
| `claude-haiku-4.5` | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | `$1.00` | `$5.00` |
| `claude-sonnet-4.6` | `global.anthropic.claude-sonnet-4-6` | `$3.00` | `$15.00` |
| `claude-sonnet-5` | `global.anthropic.claude-sonnet-5` | `$2.00` | `$10.00` |

Mức `$2/$10` của Claude Sonnet 5 là giá khuyến mại đến hết ngày 31/08/2026. Sau đó đổi code thành `$3/$15` nếu AWS không công bố mức mới. Luôn kiểm tra [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/) trước khi dùng production.

Claude Sonnet 5 là model Sonnet mới nhất. Bedrock model ID trực tiếp là `anthropic.claude-sonnet-5`; cấu hình trên dùng Global Cross-Region Inference ID `global.anthropic.claude-sonnet-5` để Lambda tại Singapore gọi được model. Lưu ý Global Cross-Region có thể xử lý dữ liệu ngoài Singapore. Nếu bắt buộc dữ liệu chỉ nằm trong một region, cần chạy Lambda và dùng model ID trực tiếp tại region được AWS hỗ trợ, chẳng hạn `us-east-1`.

Không tự tạo environment variable `AWS_REGION`: đây là key được Lambda dành riêng. Project hiện dùng quota `global`, tức một ngân sách chung cho toàn bộ proxy mỗi tháng.

## 4. IAM permissions cho Lambda role

Gắn policy tối thiểu tương tự:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
        "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT_ID:table/BedrockOpenAIProxyQuota"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "*"
    }
  ]
}
```

Bạn có thể siết `Resource` Bedrock theo model ARN nếu muốn chặt hơn.

## 5. Hoàn tất quyền dùng model trong Bedrock

Trong AWS Console:

1. Vào Amazon Bedrock.
2. Chọn region Lambda sẽ dùng, ví dụ `ap-southeast-1` (Singapore).
3. Vào Model catalog và mở Claude Sonnet 5.
4. Hoàn tất Anthropic First Time Use (FTU) form nếu account chưa từng dùng model Anthropic.

## 6. Tạo HTTP API Gateway

Tạo API:

1. API Gateway → Create API → HTTP API.
2. Add integration → Lambda → chọn function vừa tạo.
3. Routes:
   - `GET /`
   - `GET /dashboard`
   - `POST /admin/login`
   - `GET /admin/status`
   - `PUT /admin/config`
   - `PUT /admin/credentials`
   - `GET /v1/models`
   - `GET /v1/quota`
   - `POST /v1/chat/completions`
   - `OPTIONS /{proxy+}` nếu cần CORS preflight
4. Deploy stage, ví dụ `$default` hoặc `prod`.

Nếu cấu hình CORS trong API Gateway, cho phép headers `authorization,content-type,x-api-key` và methods `GET,POST,PUT,OPTIONS`.

Endpoint cuối sẽ giống:

```text
https://abc123.execute-api.ap-southeast-1.amazonaws.com/v1/chat/completions
```

## 7. Test bằng curl

```bash
curl https://YOUR_API_ID.execute-api.ap-southeast-1.amazonaws.com/v1/chat/completions \
  -H "content-type: application/json" \
  -H "authorization: Bearer test-key-1" \
  -d '{
    "model": "claude-sonnet-5",
    "messages": [
      {"role": "system", "content": "You are concise."},
      {"role": "user", "content": "Say hello in Vietnamese"}
    ],
    "max_tokens": 100,
    "temperature": 0.2
  }'
```

Nếu kiểm tra danh sách model, request cũng phải có key:

```bash
curl https://YOUR_API_ID.execute-api.ap-southeast-1.amazonaws.com/v1/models \
  -H "authorization: Bearer YOUR_API_KEY"
```

Kiểm tra quota bằng API:

```bash
curl https://YOUR_API_ID.execute-api.ap-southeast-1.amazonaws.com/v1/quota \
  -H "authorization: Bearer YOUR_API_KEY"
```

## 8. Dashboard quản trị

Đảm bảo Lambda có cả `lambda_function.py` và `dashboard.html`. Sau khi deploy, mở:

```text
https://YOUR_API_ID.execute-api.ap-southeast-1.amazonaws.com/dashboard
```

Lần đầu đăng nhập với username `admin` và bootstrap `ADMIN_PASSWORD`. Dashboard cho phép:

- Xem token, tiền, request và phần trăm ngân sách của tháng hiện tại.
- Xem lịch sử từng request: thời gian, model, input/output/total token và chi phí.
- Xem lịch sử theo tháng: request, input/output/total token, ngân sách, tiền đã dùng, còn lại và trạng thái quota.
- Lọc lịch sử theo ngày/tháng và phân trang.
- Thay đổi ngân sách USD/tháng.
- Thay đổi giới hạn input/output token trên mỗi request.
- Xem model alias và Bedrock model ID.
- Xem giá input/output, context window, output tối đa và trường hợp sử dụng khuyến nghị của từng model.
- Mở popup **Cấu hình client** để xem/copy Base URL và Model Identifier mà không làm card model quá dài.
- Bật/tắt từng model; model bị tắt sẽ trả HTTP 403 và không gọi Bedrock.
- Đổi username/mật khẩu admin.
- Nhập hoặc tự sinh API key mới; plaintext key chỉ hiện một lần.

Session admin được ký bằng HMAC, gắn với phiên bản credential, chỉ lưu trong `sessionStorage` của tab và tự hết hạn. Khi credential đổi, session cũ bị vô hiệu hóa. Ngân sách/token/model lưu tại `config#proxy`; credential hash lưu tại `auth#credentials`; usage tháng lưu tại `global#YYYY-MM`; log request lưu tại `request#YYYY-MM#TIMESTAMP#ID`.

Dashboard dùng `dynamodb:Scan` có filter prefix `global#` và `request#` để đọc lịch sử vì table hiện chỉ có partition key. Bảng request có phân trang/filter trên API để dashboard không phải render quá nhiều dòng một lúc. Các item cũ đang còn trong table sẽ tự xuất hiện; item đã bị xóa thì không thể khôi phục từ dashboard.

Ví dụ cấu hình client cho model giá rẻ:

```text
Base URL: https://YOUR_FUNCTION_URL.lambda-url.ap-southeast-1.on.aws/
Model Identifier: amazon-nova-lite
API Key: API key đang quản lý trong dashboard
```

Hoặc Claude Haiku 4.5:

```text
Model Identifier: claude-haiku-4.5
```

Nếu client yêu cầu OpenAI base URL và không tự thêm `/v1`, dùng `https://YOUR_FUNCTION_URL.lambda-url.ap-southeast-1.on.aws/v1`.

Response mẫu:

```json
{
  "id": "chatcmpl-bedrock-1784111111",
  "object": "chat.completion",
  "created": 1784111111,
  "model": "claude-sonnet-5",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Xin chào!"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 6,
    "total_tokens": 26
  }
}
```

## 9. Cách quota theo tiền/tháng hoạt động

Mỗi request dùng API key trong:

- `Authorization: Bearer ...`, hoặc
- `x-api-key: ...`

Proxy đang dùng quota global, ghi usage tháng vào item `global#YYYY-MM` và ghi chi tiết từng request vào item `request#YYYY-MM#TIMESTAMP#ID` trong DynamoDB.

Khi một request mới làm tổng chi phí dự toán vượt phần ngân sách còn lại, proxy kích hoạt circuit-breaker tháng:

- DynamoDB ghi `budget_exhausted = true` và `budget_exhausted_at` vào item `global#YYYY-MM`.
- Toàn bộ model bị tắt ở trạng thái hiệu lực; cấu hình bật/tắt thủ công vẫn được giữ nguyên.
- `/v1/models` không trả model đang khả dụng và chat trả HTTP `429 insufficient_quota`.
- Tăng ngân sách trên dashboard lên cao hơn số tiền đã dùng sẽ tự xóa khóa và khôi phục các model trước đó được bật.
- Sang tháng UTC mới, item quota mới không có khóa nên model tự hoạt động lại theo cấu hình thủ công.

Trước khi gọi Bedrock, Lambda reserve chi phí tối đa ước lượng:

```text
estimated_input_tokens + max_tokens
```

Sau khi Bedrock trả về usage thật, Lambda điều chỉnh lại số tiền thực tế. Cách này giúp tránh vượt ngân sách do nhiều request chạy song song, đổi lại đôi khi request có thể bị chặn sớm nếu `max_tokens` đặt quá cao.

Khi vượt quota, API trả về OpenAI-style error:

```json
{
  "error": {
    "message": "Monthly budget exceeded. Budget: $20.0000/month.",
    "type": "insufficient_quota",
    "param": null,
    "code": null
  }
}
```

## 10. Dùng với OpenAI SDK

Ví dụ JavaScript:

```js
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "test-key-1",
  baseURL: "https://YOUR_API_ID.execute-api.REGION.amazonaws.com/v1",
});

const result = await client.chat.completions.create({
  model: "claude-sonnet-4.6",
  messages: [{ role: "user", content: "Hello from Bedrock" }],
});

console.log(result.choices[0].message.content);
```

Ví dụ Python:

```python
from openai import OpenAI

client = OpenAI(
    api_key="test-key-1",
    base_url="https://YOUR_API_ID.execute-api.REGION.amazonaws.com/v1",
)

result = client.chat.completions.create(
    model="claude-sonnet-4.6",
    messages=[{"role": "user", "content": "Hello from Bedrock"}],
)

print(result.choices[0].message.content)
```
