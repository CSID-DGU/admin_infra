# Fail-closed validator for a v1 SelfSubjectRulesReview made by the generated
# FARM Admin reader token in ailab-operation.
#
# Resource rules are expanded as their full apiGroup/resource/verb Cartesian
# product.  This matters because a single RBAC rule grants every combination,
# not merely position-matched entries.  Wildcards, unexpected subresources,
# and resourceNames restrictions therefore cannot hide inside a combined rule.

def nonempty_string_array:
  type == "array" and length > 0 and all(.[]; type == "string" and length > 0);

def api_group_array:
  # The core API group is represented by the required empty string.
  type == "array" and length > 0 and all(.[]; type == "string");

def flattened_resource_tuples:
  [
    .status.resourceRules[] as $rule
    | $rule.apiGroups[] as $api_group
    | $rule.resources[] as $resource
    | $rule.verbs[] as $verb
    | [$api_group, $resource, $verb]
  ] | unique;

def flattened_nonresource_tuples:
  [
    (.status.nonResourceRules // [])[] as $rule
    | $rule.nonResourceURLs[] as $url
    | $rule.verbs[] as $verb
    | [$url, $verb]
  ] | unique;

def required_reader_resource_tuples:
  [
    ["", "pods", "get"],
    ["", "pods", "list"],
    ["", "pods/log", "get"],
    ["", "events", "list"],
    ["authorization.k8s.io", "selfsubjectaccessreviews", "create"],
    ["authorization.k8s.io", "selfsubjectrulesreviews", "create"],
    ["authentication.k8s.io", "selfsubjectreviews", "create"]
  ];

# jq 1.6 index()/contains() can treat a required tuple array as a nested-array
# indexing operand instead of one atomic tuple. Stream every candidate and use
# an explicit equality predicate so tuple membership remains exact.
def tuple_is_present($tuples; $required):
  any($tuples[]; . == $required);

def allowed_reader_resource_tuple:
  . as [$api_group, $resource, $verb]
  | (
      $api_group == "" and
      (
        ($resource == "pods" and ($verb == "get" or $verb == "list")) or
        ($resource == "pods/log" and $verb == "get") or
        ($resource == "events" and $verb == "list")
      )
    ) or (
      $api_group == "authorization.k8s.io" and
      ($resource == "selfsubjectaccessreviews" or $resource == "selfsubjectrulesreviews") and
      $verb == "create"
    ) or (
      $api_group == "authentication.k8s.io" and
      $resource == "selfsubjectreviews" and
      $verb == "create"
    );

def allowed_reader_nonresource_tuple:
  . as [$url, $verb]
  | $verb == "get" and (
      # system:discovery and system:public-info-viewer defaults for
      # system:authenticated. The three splatted URLs below are the only
      # explicitly permitted non-resource wildcards.
      $url == "/livez" or
      $url == "/readyz" or
      $url == "/healthz" or
      $url == "/version" or
      $url == "/version/" or
      $url == "/openapi" or
      $url == "/openapi/*" or
      $url == "/api" or
      $url == "/api/*" or
      $url == "/apis" or
      $url == "/apis/*" or
      # system:service-account-issuer-discovery default for
      # system:serviceaccounts.
      $url == "/.well-known/openid-configuration" or
      $url == "/.well-known/openid-configuration/" or
      $url == "/openid/v1/jwks" or
      $url == "/openid/v1/jwks/"
    );

(.apiVersion == "authorization.k8s.io/v1") and
(.kind == "SelfSubjectRulesReview") and
(.spec.namespace == $expected_namespace) and
(.status.incomplete == false) and
((.status.evaluationError // "") == "") and
(.status.resourceRules | type == "array" and length > 0) and
((.status.nonResourceRules // []) | type == "array") and
(all(.status.resourceRules[];
  (.apiGroups | api_group_array) and
  (.resources | nonempty_string_array) and
  (.verbs | nonempty_string_array) and
  ((.resourceNames // []) | type == "array" and length == 0) and
  all(.apiGroups[]; . != "*") and
  all(.resources[]; . != "*") and
  all(.verbs[]; . != "*")
)) and
(all((.status.nonResourceRules // [])[];
  (.nonResourceURLs | nonempty_string_array) and
  (.verbs | nonempty_string_array) and
  all(.verbs[]; . != "*")
)) and
((flattened_resource_tuples) as $resource_tuples
  | all($resource_tuples[]; allowed_reader_resource_tuple) and
    all(required_reader_resource_tuples[];
      . as $required | tuple_is_present($resource_tuples; $required))
) and
((flattened_nonresource_tuples) as $nonresource_tuples
  | all($nonresource_tuples[]; allowed_reader_nonresource_tuple)
)
