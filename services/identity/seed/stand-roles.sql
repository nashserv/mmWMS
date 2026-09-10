-- Роли стенда. Сгенерирован services/billing/seed/build-stand-seed.py вместе
-- с сидом биллинга: пользователь партнёра обязан совпадать в двух базах.
-- Люди склада выдуманы, настоящих сотрудников на стенде нет.

BEGIN;

INSERT INTO identity_role_grant (id, user_id, role_code, scope_kind, scope_id, granted_by) VALUES
    ('4a10c71d-b1dd-55d1-80fc-f48987ae4e7f', 'a479196f-9c13-5444-a778-8d5db0a443f8', 'senior_manager', 'partner_branch', '367f8fe7-e37d-59d1-b685-5a7ef0e04c07', 'сид стенда'),
    ('cefc9ea9-caf3-5012-8c28-e57e84caa3f6', 'b6622413-5e3e-5890-8cbc-950633023f86', 'account_manager', 'partner_branch', '02499f4f-4e18-56b6-879d-f3bc075cceac', 'сид стенда'),
    ('b516fd38-dcd7-5b55-9797-9b03e99abc90', '2b24b8bb-431f-532a-a8af-811bb7821e97', 'account_manager', 'partner_branch', 'a828377a-030c-5be4-ba73-7b2826446ace', 'сид стенда'),
    ('21b0abce-5f8f-5a08-8050-c13003bc4dd1', '20fba9a3-0e71-59e8-88f0-03577c1cd2e2', 'account_manager', 'partner_branch', 'd81b2c12-e214-5a43-a62d-324a0728f03c', 'сид стенда'),
    ('87f12c28-3baa-5d70-8287-619d0c78796a', '31d7b002-1e8f-5336-a197-a9b82bbf5149', 'admin', 'global', NULL, 'сид стенда'),
    ('a1940e2a-8226-5466-9fe7-dcd55ecb7701', 'ab6bffe0-14eb-5a4f-8139-9eb08c10dfbf', 'accountant', 'global', NULL, 'сид стенда'),
    ('66e36485-41a2-5711-b3ba-652c90f009fd', 'f8804b93-6c32-5364-8be4-f638d61b078c', 'warehouse_head', 'global', NULL, 'сид стенда'),
    ('3f2382f2-1562-593f-814c-5903642cae0c', '0ccd3585-b2bd-5831-80b6-092b8c24dc66', 'picker', 'global', NULL, 'сид стенда'),
    ('774c910b-ca60-559b-b09f-c8bbe4e89359', 'b76fd7c3-ebee-5014-a5c0-3c954279c0e7', 'picker', 'global', NULL, 'сид стенда'),
    ('09de965e-205e-5ca3-b90e-f1a6ef737484', '95316fa6-aeef-51e7-a345-9d7de9006c09', 'receiver', 'global', NULL, 'сид стенда'),
    ('8b174270-1688-5c61-9c3c-cbb81e6c958b', '69ea69fc-3964-56ce-84f3-58f05d287067', 'logist', 'global', NULL, 'сид стенда')
ON CONFLICT (id) DO NOTHING;

COMMIT;
