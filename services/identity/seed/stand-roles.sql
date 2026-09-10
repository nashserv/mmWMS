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
    ('8b174270-1688-5c61-9c3c-cbb81e6c958b', '69ea69fc-3964-56ce-84f3-58f05d287067', 'logist', 'global', NULL, 'сид стенда'),
    ('7125db4a-5ab4-5bab-acc5-65ba8c0c7239', '9c8dc7c5-5688-518c-848e-9cbee90df49c', 'owner', 'seller', 'stand-seller-001', 'сид стенда'),
    ('0ffdc808-9c67-5c2c-841c-eea243fd5b07', 'b905b1b4-6c0a-5f55-883a-7d5f56ab7ff1', 'owner', 'seller', 'stand-seller-002', 'сид стенда'),
    ('12f9f808-98b8-5f2c-810c-d8556b5681dc', '4986666b-65f3-5d51-b219-8561eb6ec970', 'owner', 'seller', 'stand-seller-003', 'сид стенда'),
    ('2e6cad55-3285-5bfd-80f8-a4221f8a304b', '958b1edb-41c4-550c-905e-65e51ac95381', 'owner', 'seller', 'stand-seller-004', 'сид стенда'),
    ('cfbba50d-b4ee-567d-a533-25080492294f', '86aa9ecd-74fa-5525-a8e4-64be5c8b596f', 'owner', 'seller', 'stand-seller-005', 'сид стенда'),
    ('1795e4a0-e600-5682-9886-6bb4c3dbf5fd', '48d236b1-1ed6-50b3-802b-1c2fece2762c', 'owner', 'seller', 'stand-seller-006', 'сид стенда'),
    ('539768a6-0610-5fb9-b31a-0984101ebd68', '24743d97-ef33-50f0-8dfd-ab4fba522b6c', 'owner', 'seller', 'stand-seller-007', 'сид стенда'),
    ('4de87d16-003c-542f-b035-70c5f27af9f2', 'f92a6ee1-3f64-5629-9d42-84299bc1e2c3', 'owner', 'seller', 'stand-seller-008', 'сид стенда'),
    ('4166d7b9-fbf3-5257-b7a5-7933f164d9e2', '649f62c4-c102-5ea4-aad3-664815dc1196', 'owner', 'seller', 'stand-seller-009', 'сид стенда'),
    ('2f955cfe-3299-56aa-b7c5-bd6bb45939e7', 'c29fbde0-7554-538c-a518-369a0e7b61a6', 'owner', 'seller', 'stand-seller-010', 'сид стенда'),
    ('40cb16f9-3cd0-521b-a109-433230bd33b2', '22fb6f05-71c7-5268-aca4-529f4af055ae', 'owner', 'seller', 'stand-seller-011', 'сид стенда'),
    ('5c5e82b9-b79b-5a07-9b51-c5004a53d6ee', '9e5384a1-50ee-5eb1-a5db-3a8ffbe022c1', 'owner', 'seller', 'stand-seller-012', 'сид стенда'),
    ('5c570db9-e70f-5e54-b14e-13258bacc6c9', '1f926a14-5769-5b07-ab3e-4778422b7dd8', 'owner', 'seller', 'stand-seller-013', 'сид стенда'),
    ('e4a6abe2-c909-5720-928a-95471ca0f983', '88785005-7314-5deb-806f-f4087eee8446', 'owner', 'seller', 'stand-seller-014', 'сид стенда'),
    ('6f955cbb-b826-5135-9674-d3041320b77b', '0dfb8608-8a52-5b6d-ac58-454ab0ebc106', 'owner', 'seller', 'stand-seller-015', 'сид стенда'),
    ('afb6a9ca-a76e-5ffa-8167-e6234c7c8fd3', 'f32d046c-8ed6-5391-a270-dd9ffeb28b19', 'owner', 'seller', 'stand-seller-016', 'сид стенда'),
    ('2f513cd5-4ed3-53ab-bde1-99120d7b80a7', '3e28a3c4-7a09-591e-8aa4-37fdbad4122f', 'owner', 'seller', 'stand-seller-017', 'сид стенда'),
    ('45910198-2b3c-578e-ad5a-511a578d533a', 'ab0f1a01-4ae8-5cbb-abf4-6fe17f320e36', 'owner', 'seller', 'stand-seller-018', 'сид стенда'),
    ('1d7686ab-bd44-5d2e-952c-48f16fa036c6', '37b6a60a-27ff-51a5-b9fd-ac5338f4d35e', 'owner', 'seller', 'stand-seller-019', 'сид стенда'),
    ('c59349d3-6f1a-5088-a916-8c8237a33fdb', 'e8637bb1-364e-5487-af0c-bd203155aaac', 'owner', 'seller', 'stand-seller-020', 'сид стенда'),
    ('5cb8c464-45f8-5f6e-b2b8-e73758b2e714', 'eb19800f-e09b-5843-8351-ac5317aa0c21', 'owner', 'seller', 'stand-seller-021', 'сид стенда'),
    ('025a0ab7-1b8b-508c-9c94-2c79f11e46aa', '7b53f152-4f4b-5a75-970d-b1d3fbc94057', 'owner', 'seller', 'stand-seller-022', 'сид стенда')
ON CONFLICT (id) DO NOTHING;

COMMIT;
