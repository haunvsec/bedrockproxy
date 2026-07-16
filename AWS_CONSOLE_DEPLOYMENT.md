# Triển khai Bedrock OpenAI Proxy hoàn toàn bằng AWS Console

Tài liệu này dùng **chỉ giao diện web AWS Console**, không cần AWS CLI, SAM, CDK hay Terraform. Các bước được viết cho region Singapore `ap-southeast-1` và HTTP API Gateway.

## 0. Kết quả sau khi triển khai

Bạn sẽ có:

- Một Lambda chạy proxy OpenAI-compatible và phục vụ dashboard HTML.
- Một DynamoDB On-Demand lưu quota tháng, cấu hình và credential đã hash.
- Một HTTP API Gateway public HTTPS.
- Xác thực API client bằng API key hash lưu trong DynamoDB.
- Đăng nhập dashboard bằng username/password hash lưu trong DynamoDB.
- Claude Sonnet 5 qua Global Cross-Region Inference từ Singapore.

Các file cần dùng:

- [lambda_function.py](/Volumes/DATA/openai_bedrock/lambda_function.py)
- [dashboard.html](/Volumes/DATA/openai_bedrock/dashboard.html)

## 1. Chọn Singapore

1. Đăng nhập [AWS Management Console](https://console.aws.amazon.com/).
2. Trên thanh trên cùng, mở region selector.
3. Chọn **Asia Pacific (Singapore) — ap-southeast-1**.
4. Giữ nguyên region này khi tạo DynamoDB, Lambda và API Gateway.

## 2. Tạo DynamoDB

1. Mở **DynamoDB** trong AWS Console.
2. Chọn **Tables → Create table**.
3. Nhập:
   - **Table name:** `BedrockOpenAIProxyQuota`
   - **Partition key:** `quota_id`
   - **Data type:** `String`
4. Không tạo sort key.
5. Trong **Table settings**, chọn **Customize settings** nếu cần và đặt capacity là **On-demand**.
6. Giữ encryption mặc định **AWS owned key**.
7. Không cần secondary index, DynamoDB Streams, Global Tables, PITR hoặc backup cho bản proxy nhỏ này.
8. Chọn **Create table** và chờ status thành **Active**.
9. Mở tab **General information/Overview**, lưu lại Table ARN. ARN có dạng:

```text
arn:aws:dynamodb:ap-southeast-1:YOUR_ACCOUNT_ID:table/BedrockOpenAIProxyQuota
```

DynamoDB On-Demand tính phí theo request và không cần chạy database server. Xem [DynamoDB table operations](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithTables.Basics.html).

Table vẫn chỉ cần partition key `quota_id`. Lambda tự thêm các thuộc tính khác theo từng loại item:

- `auth#credentials`: username, password hash, API key hash, hint và phiên bản credential.
- `config#proxy`: ngân sách, giới hạn token và trạng thái model.
- `global#YYYY-MM`: ngân sách, tiền, request count, input/output token và trạng thái quota của từng tháng. Mỗi tháng là một item riêng nên table tự lưu lịch sử.
- `request#YYYY-MM#TIMESTAMP#ID`: log chi tiết từng request gồm thời gian, model, input/output token và chi phí thực tế.

Không tạo thêm cột hoặc index trong màn hình **Create table**.

## 3. Tạo Lambda

1. Mở **Lambda → Functions → Create function**.
2. Chọn **Author from scratch**.
3. Điền:
   - **Function name:** `bedrock-openai-proxy`
   - **Runtime:** Python 3.12
   - **Architecture:** arm64 hoặc x86_64; cả hai đều dùng được vì project không có binary dependency.
4. Trong **Permissions**, chọn **Create a new role with basic Lambda permissions**.
5. Chọn **Create function**.

Lambda console hỗ trợ chỉnh Python và thêm nhiều file trực tiếp trong code editor. Tham khảo [AWS Lambda ZIP/code editor](https://docs.aws.amazon.com/lambda/latest/dg/configuration-function-zip.html).

### 3.1 Dán source bằng code editor

1. Trong function vừa tạo, mở tab **Code**.
2. Trong cây file bên trái, mở `lambda_function.py`.
3. Xóa code Hello World và dán toàn bộ nội dung file [lambda_function.py](/Volumes/DATA/openai_bedrock/lambda_function.py).
4. Trong file explorer của code editor, chọn biểu tượng **New file**.
5. Đặt tên chính xác `dashboard.html`.
6. Dán toàn bộ nội dung file [dashboard.html](/Volumes/DATA/openai_bedrock/dashboard.html).
7. Chọn **Deploy**. Chỉ Save file mà chưa Deploy thì Lambda vẫn chạy version code cũ.

Hai file phải nằm ở root của deployment package:

```text
lambda_function.py
dashboard.html
```

### 3.2 Kiểm tra handler

1. Trong tab **Code**, kéo xuống **Runtime settings**.
2. Chọn **Edit**.
3. Handler phải là:

```text
lambda_function.lambda_handler
```

4. Chọn **Save**.

Quy tắc đặt handler được mô tả tại [Python Lambda handler](https://docs.aws.amazon.com/lambda/latest/dg/python-handler.html).

### 3.3 Cấu hình memory và timeout

1. Mở **Configuration → General configuration → Edit**.
2. Đặt:
   - **Memory:** `512 MB`
   - **Timeout:** `5 min 0 sec`
3. Chọn **Save**.

Lambda cho phép timeout tối đa 900 giây (15 phút). Hướng dẫn này dùng 300 giây để Bedrock có thời gian hoàn tất và Lambda ghi nhận quota. Xem [Configure Lambda timeout](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html).

Lưu ý: HTTP API Gateway có integration timeout tối đa 30 giây và không tăng được. Lambda timeout 5 phút giúp Lambda hoàn tất xử lý/ghi quota nếu API Gateway đã timeout, nhưng client vẫn có thể nhận `504` nếu model trả lời quá 30 giây. Xem [HTTP API quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html).

## 4. Environment variables

1. Mở **Configuration → Environment variables → Edit**.
2. Chọn **Add environment variable** cho từng dòng dưới đây.
3. Lần triển khai đầu, thêm bốn secret dưới đây; thay giá trị mẫu bằng chuỗi ngẫu nhiên mạnh do password manager tạo.

| Key | Value mẫu |
|---|---|
| `API_KEY` | `client-key-ngau-nhien-rat-dai` |
| `ADMIN_PASSWORD` | `mat-khau-admin-ngau-nhien-rat-dai` |
| `ADMIN_SESSION_SECRET` | `chuoi-ngau-nhien-it-nhat-32-ky-tu` |
| `CREDENTIAL_HASH_KEY` | `chuoi-ngau-nhien-khac-it-nhat-32-ky-tu` |

4. Chọn **Save**.

`API_KEY` phải dài ít nhất 24 ký tự, `ADMIN_PASSWORD` ít nhất 12 ký tự. Username bootstrap là `admin`. `ADMIN_SESSION_SECRET` và `CREDENTIAL_HASH_KEY` phải là hai chuỗi khác nhau, mỗi chuỗi ít nhất 32 byte. Session mặc định 8 giờ. Không thêm `AWS_REGION`; Lambda tự tạo biến này và không cho phép override.

`API_KEY` và `ADMIN_PASSWORD` chỉ tồn tại tạm thời để khởi tạo database. Lần gọi Lambda đầu tiên ghi item `auth#credentials` chứa API key HMAC và password PBKDF2 có khóa. Sau khi test login thành công, thực hiện mục 7.4 để xóa hai plaintext secret này khỏi env.

Giữ `CREDENTIAL_HASH_KEY` ổn định và sao lưu trong password manager. Nếu đổi hoặc làm mất key này, các hash hiện có không thể xác minh và admin/API client sẽ không đăng nhập được. Thay `ADMIN_SESSION_SECRET` sẽ vô hiệu hóa toàn bộ session admin đang mở.

Region, Bedrock timeout, DynamoDB table, model map, quota scope và giá token được đặt bằng constants ở đầu `lambda_function.py`. Ngân sách USD/tháng, input/output token limit và trạng thái model chỉnh trong dashboard, sau đó được lưu tại item `config#proxy` trong DynamoDB. Username/password/API key quản lý trong dashboard và hash lưu tại `auth#credentials`.

Giá `$2/$10` trên một triệu input/output token là giá khuyến mại Claude Sonnet 5 đến hết **31/08/2026**. Từ **01/09/2026**, theo công bố hiện tại của AWS, đổi thành:

```text
DEFAULT_MODEL_PRICING = {
    "global.anthropic.claude-sonnet-5": {"input": 3.00, "output": 15.00}
}
```

Luôn kiểm tra [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/) trước khi dùng production.

## 5. Cấp IAM permission cho Lambda

Role mặc định đã có quyền ghi CloudWatch Logs. Bổ sung Bedrock và DynamoDB:

1. Trong Lambda, mở **Configuration → Permissions**.
2. Trong **Execution role**, chọn link tên role. AWS mở IAM console.
3. Chọn **Add permissions → Create inline policy**.
4. Chọn tab **JSON**.
5. Dán policy dưới đây; thay `YOUR_ACCOUNT_ID` bằng Account ID của bạn:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeBedrockModels",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ReadWriteProxyQuota",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:ap-southeast-1:YOUR_ACCOUNT_ID:table/BedrockOpenAIProxyQuota"
    }
  ]
}
```

6. Chọn **Next**.
7. Policy name: `BedrockOpenAIProxyAccess`.
8. Chọn **Create policy**.

`bedrock:InvokeModel` dùng cho request non-streaming. `bedrock:InvokeModelWithResponseStream` dùng khi client gửi `stream=true`, ví dụ extension Cline trên VSCode. `Resource: "*"` cho Bedrock được dùng để tránh thiếu quyền với Global Cross-Region Inference Profile và các model đích. Nếu AWS Organizations có SCP chặn một region đích, policy `Allow` ở role không thể ghi đè explicit deny đó.

## 6. Hoàn tất quyền dùng các model

1. Mở **Amazon Bedrock** trong Singapore.
2. Vào **Model catalog**.
3. Tìm **Claude Sonnet 5**.
4. Mở model và hoàn tất **First Time Use (FTU)** form nếu AWS yêu cầu.
5. Làm tương tự với **Claude Haiku 4.5**, **Claude Sonnet 4.6** và **Amazon Nova 2 Lite** nếu AWS yêu cầu.
6. Điền use case và website/project URL, chấp nhận điều khoản phù hợp.

Anthropic yêu cầu FTU một lần cho account hoặc AWS Organization trước lần invoke đầu tiên; AWS cho biết access được cấp ngay sau khi form hợp lệ. Xem [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).

Proxy dùng:

```text
global.amazon.nova-2-lite-v1:0
global.anthropic.claude-haiku-4-5-20251001-v1:0
global.anthropic.claude-sonnet-4-6
global.anthropic.claude-sonnet-5
```

Các Model Identifier mà client gửi tới proxy:

```text
amazon-nova-lite
claude-haiku-4.5
claude-sonnet-4.6
claude-sonnet-5
```

Lambda và endpoint vẫn ở Singapore, nhưng **Global Cross-Region Inference có thể xử lý dữ liệu ngoài Singapore**. Đây không phải cấu hình strict Singapore data residency.

## 7. Test Lambda ngay trong Console

Nên test Lambda trước khi tạo API Gateway để tách lỗi backend khỏi lỗi routing.

### 7.1 Test dashboard file

1. Trong Lambda, mở tab **Test**.
2. Chọn **Create new event**.
3. Event name: `DashboardGet`.
4. Dán:

```json
{
  "version": "2.0",
  "rawPath": "/dashboard",
  "requestContext": {
    "http": {
      "method": "GET",
      "path": "/dashboard"
    }
  }
}
```

5. Chọn **Save**, sau đó **Test**.
6. Kết quả đúng có `statusCode: 200`, header `content-type: text/html` và body bắt đầu bằng `<!doctype html>`.

Nếu nhận `500`, mở log output. Lỗi thường gặp nhất là chưa tạo `dashboard.html`, sai tên file, hoặc chưa bấm Deploy.

### 7.2 Test đăng nhập admin

Tạo event `AdminLogin`, thay password đúng với environment variable:

```json
{
  "version": "2.0",
  "rawPath": "/admin/login",
  "headers": {
    "content-type": "application/json"
  },
  "requestContext": {
    "http": {
      "method": "POST",
      "path": "/admin/login"
    }
  },
  "body": "{\"username\":\"admin\",\"password\":\"MAT_KHAU_ADMIN_CUA_BAN\"}",
  "isBase64Encoded": false
}
```

Kết quả đúng là HTTP 200 và có `token`. Đồng thời DynamoDB xuất hiện item `auth#credentials`. HTTP 401 nghĩa là sai username/password. HTTP 500 về admin configuration thường là thiếu bootstrap credential, thiếu key hoặc key ngắn hơn 32 byte.

### 7.3 Test chat, DynamoDB và Bedrock cùng lúc

Tạo event `ChatCompletion`, thay `YOUR_CLIENT_API_KEY` bằng `API_KEY`:

```json
{
  "version": "2.0",
  "rawPath": "/v1/chat/completions",
  "headers": {
    "content-type": "application/json",
    "authorization": "Bearer YOUR_CLIENT_API_KEY"
  },
  "requestContext": {
    "http": {
      "method": "POST",
      "path": "/v1/chat/completions"
    }
  },
  "body": "{\"model\":\"claude-sonnet-5\",\"messages\":[{\"role\":\"user\",\"content\":\"Tra loi dung mot tu: OK\"}],\"max_tokens\":20}",
  "isBase64Encoded": false
}
```

Kết quả đúng:

- `statusCode: 200`.
- Body có `choices`, `usage.prompt_tokens`, `usage.completion_tokens`.
- DynamoDB xuất hiện item `global#YYYY-MM` và item log `request#YYYY-MM#TIMESTAMP#ID`.

### 7.4 Xóa plaintext bootstrap secret

Chỉ làm bước này sau khi login và chat test đều thành công:

1. DynamoDB → Tables → `BedrockOpenAIProxyQuota` → **Explore table items**.
2. Chọn **Run scan** và xác nhận có item `quota_id = auth#credentials`.
3. Lambda → `bedrock-openai-proxy` → **Configuration → Environment variables → Edit**.
4. Xóa hai dòng `API_KEY` và `ADMIN_PASSWORD`.
5. Giữ lại `CREDENTIAL_HASH_KEY` và `ADMIN_SESSION_SECRET`.
6. Chọn **Save**, test đăng nhập lại và gọi chat bằng API key cũ.

Database chỉ chứa `admin_password_hash` và `api_key_hash`; không chứa plaintext. Không xóa `auth#credentials` sau khi đã xóa bootstrap env, nếu không hệ thống sẽ không thể tự khởi tạo lại.

## 8. Tạo HTTP API Gateway

1. Mở **API Gateway → APIs → Create API**.
2. Tại **HTTP API**, chọn **Build**. Không chọn REST API hoặc WebSocket API.
3. Chọn **Add integration → Lambda**.
4. Chọn region Singapore và function `bedrock-openai-proxy`.
5. API name: `bedrock-openai-proxy-api`.
6. Hoàn tất wizard bằng **Review and create → Create**.

AWS mô tả luồng console tại [Create an HTTP API](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-develop.html), và Lambda proxy integration mặc định dùng payload format mới nhất trong console tại [HTTP API Lambda integration](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-develop-integrations-lambda.html).

### 8.1 Dùng một `$default` route

Code Lambda đã tự route theo path, nên chỉ cần một catch-all route:

1. Trong HTTP API vừa tạo, mở **Routes**.
2. Chọn **Create**.
3. Route key chọn/nhập **`$default`**.
4. Chọn **Create**.
5. Chọn route `$default`, chọn **Attach integration**.
6. Chọn Lambda integration trỏ tới `bedrock-openai-proxy`.
7. Nếu wizard đã tạo route thừa theo tên function, có thể xóa route đó sau khi `$default` hoạt động.

`$default` bắt mọi method/path chưa có route cụ thể. Xem [HTTP API routes](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-develop-routes.html).

### 8.2 Stage

1. Mở **Stages**.
2. Dùng stage **`$default`**.
3. Bật **Auto-deploy**.
4. Lưu thay đổi nếu console yêu cầu.

Với `$default` stage, dashboard URL không có `/prod`:

```text
https://API_ID.execute-api.ap-southeast-1.amazonaws.com/dashboard
```

### 8.3 CORS

Dashboard được Lambda phục vụ cùng origin với API nên **không bắt buộc cấu hình CORS trong API Gateway**.

Chỉ khi gọi API từ một website domain khác, vào **CORS → Configure** và thêm:

- Allow origins: domain web cụ thể; tạm thời có thể dùng `*`.
- Allow headers: `authorization`, `content-type`, `x-api-key`.
- Allow methods: `GET`, `POST`, `PUT`, `OPTIONS`.

Khi bật CORS tại HTTP API, API Gateway sẽ bỏ qua CORS headers từ Lambda và dùng cấu hình của API Gateway. Xem [HTTP API CORS](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-cors.html).

## 9. Kiểm tra dashboard thật

1. Trong API Gateway, copy **Invoke URL**.
2. Mở tab browser mới:

```text
INVOKE_URL/dashboard
```

3. Base URL trên form login phải tự điền đúng Invoke URL.
4. Đăng nhập bằng username `admin` và giá trị `ADMIN_PASSWORD`.
5. Xác nhận dashboard hiển thị:
   - Tháng hiện tại theo UTC.
   - Ngân sách, tiền đã dùng/còn lại.
   - Input/output/total token.
   - Số request.
   - Danh sách model.
   - Giá input/output, context, output tối đa và hướng dẫn cấu hình client trên từng model.
6. Thử đổi ngân sách USD/tháng và giảm output token, chọn **Lưu cấu hình**, refresh trang và kiểm tra các giá trị vẫn còn.
7. Tắt `claude-sonnet-5`, chọn **Lưu trạng thái model**.
8. Chạy lại event chat: phải nhận HTTP 403 và Bedrock không được gọi.
9. Bật lại model và lưu.
10. Trong **Tài khoản admin và API key**, nhập mật khẩu hiện tại rồi chọn **Tự sinh API key mạnh**.
11. Lưu API key được hiển thị vào password manager; key này chỉ hiện một lần.
12. Kiểm tra key cũ trả HTTP 401 và key mới gọi được `/v1/models`.
13. Có thể đổi username/mật khẩu admin tại cùng khu vực. Các session admin cũ sẽ bị vô hiệu hóa.
14. Tại model `amazon-nova-lite` hoặc `claude-haiku-4.5`, chọn **Cấu hình client** và kiểm tra popup hiển thị đúng Base URL, Model Identifier và API Key dạng ẩn.
15. Trong **Lịch sử sử dụng theo request**, lọc theo ngày/tháng và kiểm tra request vừa chạy có model, input/output token và chi phí.
16. Trong **Lịch sử sử dụng theo tháng**, lọc theo tháng và kiểm tra các item `global#YYYY-MM` cũ được hiển thị theo thứ tự mới nhất trước.

### 9.1 Xác nhận Lambda URL đang chạy đúng phiên bản

Bản dashboard mới hiển thị cuối trang:

```text
UI 2026.07.15-usage-history-v8 · Backend 2026.07.16-bedrock-stream-v10
```

Nếu vẫn không thấy đủ bốn model `amazon-nova-lite`, `claude-haiku-4.5`, `claude-sonnet-4.6`, `claude-sonnet-5`, bạn đang chạy code cũ:

1. Lambda → tab **Code**: thay đúng cả `lambda_function.py` và `dashboard.html`.
2. Chọn **Deploy**; chỉ Save file là chưa đủ.
3. Nếu Function URL gắn với một Lambda alias/version, publish version mới và chuyển alias sang version mới. Deploy trong code editor chỉ cập nhật `$LATEST`.
4. Mở lại Function URL với `/dashboard` và hard refresh trình duyệt.
5. Xác nhận footer có cùng UI/Backend version như trên và danh sách có bốn Model Identifier.

### 9.2 Tự động khóa model khi hết ngân sách

Khi reservation của request mới làm tổng dự toán vượt ngân sách còn lại, Lambda ghi vào item tháng:

```text
budget_exhausted = true
budget_exhausted_at = <Unix timestamp>
```

Dashboard sẽ hiển thị banner khóa ngân sách và trạng thái **TỰ ĐỘNG TẮT DO HẾT NGÂN SÁCH** trên toàn bộ model. Công tắc model bị khóa để không ghi đè cấu hình thủ công. Tăng budget lên cao hơn `spent_usd` rồi chọn **Lưu cấu hình** sẽ tự mở lại những model vốn được bật. Tháng UTC mới dùng item `global#YYYY-MM` mới nên circuit-breaker tự reset.

### 9.3 Lịch sử sử dụng

Dashboard có hai bảng lịch sử:

- **Lịch sử sử dụng theo request** đọc item `request#YYYY-MM#TIMESTAMP#ID`, hiển thị thời gian request, model, Bedrock ID, input/output/total token và chi phí USD.
- **Lịch sử sử dụng theo tháng** đọc item `global#YYYY-MM`, hiển thị request, input/output/total token, ngân sách, đã dùng, còn lại, tỷ lệ sử dụng và trạng thái quota.

Cả hai bảng đều có filter theo ngày/tháng và phân trang. Không cần thay partition key, tạo sort key, index hoặc migrate table. Các item cũ đang còn trong DynamoDB sẽ xuất hiện ngay sau khi deploy. Nếu item đã bị xóa thì dashboard không thể tự dựng lại dữ liệu. Lambda role bắt buộc có quyền `dynamodb:Scan`.

## 10. Debug theo triệu chứng

| Triệu chứng | Nguyên nhân thường gặp | Cách kiểm tra/sửa trên Console |
|---|---|---|
| API trả `{"message":"Not Found"}` | Chưa có route hoặc sai stage | API Gateway → Routes: kiểm tra `$default`; Stages: `$default` + Auto-deploy |
| API Gateway `502 Bad Gateway` | Lambda exception hoặc response lỗi | Lambda → Monitor → View CloudWatch logs; chạy lại Lambda Test event |
| Dashboard HTTP 500 | Thiếu `dashboard.html`, sai tên hoặc chưa Deploy | Lambda → Code: kiểm tra hai file ở root và chọn Deploy |
| Login HTTP 500 | Chưa bootstrap `auth#credentials`, thiếu hash/session key hoặc key quá ngắn | Kiểm tra env và DynamoDB item `auth#credentials` |
| Login báo role không truy cập được DynamoDB | Inline policy thiếu `GetItem`/`UpdateItem`, ARN sai account/region/table | Lambda → Configuration → Permissions → execution role → policy `BedrockOpenAIProxyAccess` |
| Login được nhưng lịch sử báo lỗi tải dữ liệu | Inline policy thiếu `dynamodb:Scan` | Thêm `dynamodb:Scan` vào policy `BedrockOpenAIProxyAccess`, lưu policy rồi thử **Làm mới** |
| Login HTTP 401 | Sai username/password trong database | Dùng credential mới nhất đã lưu từ dashboard |
| `/v1/*` HTTP 401 | API key sai hoặc đã được rotate | Kiểm tra header và API key mới nhất trong password manager |
| Login/API đều lỗi sau khi đổi env key | `CREDENTIAL_HASH_KEY` không còn khớp hash trong database | Khôi phục đúng key cũ; không tự ý rotate key này |
| `AccessDeniedException` từ DynamoDB | Role thiếu `GetItem`/`UpdateItem`/`Scan` hoặc ARN sai | Lambda → Permissions → role → inline policy |
| `ResourceNotFoundException` DynamoDB | Table chưa tạo, sai tên constant hoặc tạo ở region khác | Kiểm tra constant `QUOTA_TABLE_NAME` và table phải ở Singapore |
| `AccessDeniedException` từ Bedrock | Role thiếu `bedrock:InvokeModel`, FTU chưa hoàn tất hoặc model/region đích bị SCP chặn | Kiểm tra policy `BedrockOpenAIProxyAccess`, Bedrock Model catalog/FTU và AWS Organizations SCP |
| `ValidationException` về model ID | Sai `DEFAULT_MODEL_MAP` hoặc profile chưa khả dụng | Dùng chính xác `global.anthropic.claude-sonnet-5`, region Singapore |
| HTTP 403 `model_disabled` | Model đang bị tắt trong dashboard | Dashboard → Danh sách model → bật lại → Lưu trạng thái model |
| HTTP 429 `insufficient_quota` | Đã hết budget hoặc reservation của request quá lớn | Dashboard xem remaining; giảm `max_tokens` hoặc tăng ngân sách USD/tháng rồi lưu |
| Dashboard luôn bằng 0 | Chưa có request thành công hoặc request đi ngoài proxy | Chạy Lambda chat test, kiểm tra item `global#YYYY-MM` và `request#YYYY-MM#...` trong DynamoDB |
| Tiền dashboard không khớp AWS Bill | Pricing JSON cũ, request đi ngoài proxy hoặc loại phí không được proxy tính | Cập nhật pricing; đối chiếu Bedrock Usage/Cost Explorer; bắt buộc mọi client đi qua proxy |
| API Gateway `504` | Bedrock chạy quá giới hạn 30 giây của HTTP API | Với Cline/VSCode nên dùng Lambda Function URL thay vì HTTP API Gateway; giảm output/max_tokens nếu vẫn quá lâu |
| Lambda báo `Task timed out` | General configuration timeout còn thấp | Lambda → Configuration → General configuration → Edit → Timeout `5 min 0 sec` |
| Lỗi reserved environment key | Đã tự thêm `AWS_REGION` | Xóa `AWS_REGION`; Lambda tự cung cấp biến này |

### Xem CloudWatch Logs

1. Lambda → function `bedrock-openai-proxy`.
2. Mở **Monitor → View CloudWatch logs**.
3. Chọn log stream mới nhất.
4. Tìm traceback, `AccessDeniedException`, `ResourceNotFoundException`, `ValidationException` hoặc timeout.

## 11. Giới hạn cần hiểu rõ

- Đây là quota ở tầng ứng dụng, không phải hard cap của hóa đơn AWS toàn account.
- Request gọi Bedrock trực tiếp, request qua proxy khác hoặc một số request lỗi có thể phát sinh chi phí ngoài số dashboard.
- Input limit dùng ước lượng ký tự trước khi gọi model; output limit được áp trực tiếp qua Bedrock `maxTokens`.
- Usage token sau request lấy trực tiếp từ Bedrock và là số dùng để điều chỉnh tiền đã reserve.
- Nếu Bedrock đã trả kết quả nhưng bước finalize DynamoDB lỗi, code giữ nguyên reservation bảo thủ để tránh ghi thiếu chi phí.
- Tháng quota dùng UTC (`YYYY-MM`), không dùng múi giờ Việt Nam.
- HTTP API giới hạn 30 giây. `stream=true` dùng Bedrock `ConverseStream`, nhưng nếu đi qua HTTP API Gateway client vẫn có thể gặp giới hạn timeout của API Gateway; Cline nên dùng Lambda Function URL.
- Bedrock SDK read timeout mặc định là 240 giây, đặt bằng constant `BEDROCK_READ_TIMEOUT_SECONDS`.
- Global Cross-Region Inference không đảm bảo dữ liệu chỉ được xử lý trong Singapore.

## 12. Checklist trước production

- [ ] DynamoDB Active tại `ap-southeast-1`, partition key `quota_id` kiểu String.
- [ ] Lambda có cả `lambda_function.py` và `dashboard.html`, đã chọn Deploy.
- [ ] Lambda General configuration: memory 512 MB, timeout 5 phút.
- [ ] Không tự cấu hình `AWS_REGION`.
- [ ] Bootstrap API key/password đủ dài; hash key và session secret khác nhau, tối thiểu 32 byte.
- [ ] Item `auth#credentials` đã được tạo; đã xóa plaintext `API_KEY`/`ADMIN_PASSWORD` khỏi env sau khi test.
- [ ] `CREDENTIAL_HASH_KEY` đã được sao lưu an toàn và không bị thay đổi.
- [ ] Lambda role có Bedrock InvokeModel/InvokeModelWithResponseStream và DynamoDB GetItem/UpdateItem/Scan.
- [ ] Anthropic FTU đã hoàn tất.
- [ ] Lambda Test: dashboard, login và chat đều thành công.
- [ ] API Gateway có `$default` route, Lambda integration và `$default` auto-deploy stage.
- [ ] Dashboard login được và cấu hình model/token lưu qua refresh.
- [ ] Dashboard đã lưu được ngân sách tháng, giới hạn token và trạng thái model.
- [ ] Đã thử rotate API key và đổi password admin từ dashboard.
- [ ] Pricing JSON đúng với ngày triển khai.
- [ ] Đã thử quota 429 và model-disabled 403.
- [ ] Đã đặt AWS Budget/Cost Anomaly Detection riêng để cảnh báo hóa đơn ngoài proxy.
